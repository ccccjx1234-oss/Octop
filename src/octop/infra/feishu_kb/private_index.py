"""Owner-scoped, live-authorized Feishu semantic index.

The mapping survives source/grant deletion so failed cross-store cleanup is retryable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from octop.infra.feishu_kb.service import FeishuKbService
from octop.infra.knowledge.chunk import chunk_text
from octop.infra.knowledge.embed import embed_knowledge_texts
from octop.infra.knowledge.gate import assert_knowledge_usable
from octop.infra.knowledge.params import get_advanced_settings
from octop.infra.knowledge.plane import open_knowledge_index, postgres_database
from octop.infra.utils.paths import PathLayout

log = logging.getLogger(__name__)
PREFIX = "private-feishu:"


def collection(user: int, source: str, digest: str | None = None, config_version: str | None = None, generation: int | None = None) -> str:
    key = f"{user}:{source}" if digest is None else json.dumps([user, source, digest, config_version, generation], separators=(",", ":"))
    return "feishu-private-" + hashlib.sha256(key.encode()).hexdigest()


@contextmanager
def _source_lock(services: Any, user: int, source: str) -> Iterator[None]:
    """Serialize rebuild and reads across processes without locking the control DB."""
    key = collection(user, source)
    if services.db.dialect == "postgresql":
        import psycopg

        database = postgres_database(services)
        if database is None:
            raise RuntimeError("PostgreSQL knowledge configuration unavailable")
        lock_id = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
        with psycopg.connect(database.postgresql_conninfo(), autocommit=True) as conn:
            conn.execute("SELECT pg_advisory_lock(%s)", (lock_id,))
            try:
                yield
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
    else:
        lock_path = PathLayout.from_env().knowledge_dir / key / "private-lock.sqlite"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(lock_path, timeout=30, isolation_level=None) as conn:
            conn.execute("BEGIN EXCLUSIVE")
            try:
                yield
            finally:
                conn.execute("ROLLBACK")


def _valid(
    service: FeishuKbService,
    user: int,
    source: str,
    digest: str | None = None,
    generation: int | None = None,
) -> bool:
    row = service.repo.source(source, user)
    if not row or not row["enabled"] or not row["retrieval_enabled"]:
        return False
    grant = service.repo.grant(user, int(row["provider_id"]))
    if not grant or grant["refreshing"]:
        return False
    try:
        provider = service._provider(int(row["provider_id"]))
        if not service._subject_matches(grant["subject"], provider.client_id):
            return False
    except Exception:
        return False
    return (digest is None or row["digest"] == digest) and (
        generation is None or row["retrieval_generation"] == generation
    )


def _delete(services: Any, user: int, source: str) -> None:
    open_knowledge_index(services, collection(user, source)).delete_doc(source)


def invalidate(services: Any, user: int, source: str) -> None:
    """Retain tombstones for writers that resume after a cleanup pass."""
    service = FeishuKbService(services)
    try:
        _delete(services, user, source)  # legacy shared collection
    except Exception:
        log.warning("legacy private index cleanup deferred", exc_info=True)
    with services.db.connect() as c:
        rows = list(c.execute("SELECT * FROM feishu_kb_private_index_versions WHERE source_id=? AND user_id=?", (source, user)).fetchall())
    for row in rows:
        if (_valid(service, user, source, row["digest"], int(row["generation"]))
                and version(services) == row["config_version"]):
            continue
        try:
            open_knowledge_index(services, row["collection_id"]).delete_doc(source)
            with services.db.transaction() as c:
                c.execute("UPDATE feishu_kb_private_index_versions SET ready=0 WHERE collection_id=?", (row["collection_id"],))
        except Exception:
            log.warning("private index cleanup deferred", exc_info=True)


def cleanup(services: Any) -> None:
    with services.db.connect() as c:
        rows = list(c.execute("SELECT source_id,user_id FROM feishu_kb_private_index UNION SELECT source_id,user_id FROM feishu_kb_private_index_versions").fetchall())
    for row in rows:
        invalidate(services, int(row["user_id"]), str(row["source_id"]))


def catalog(services: Any, user: int) -> list[dict[str, str]]:
    service = FeishuKbService(services)
    return [
        {
            "id": PREFIX + str(row["source_id"]),
            "name": "Private Feishu " + str(row["source_id"]),
            "description": "Owner-only document",
        }
        for row in service.repo.sources(user)
        if row["enabled"]
        and row["retrieval_enabled"]
        and _valid(service, user, str(row["source_id"]))
    ]


def version(services: Any) -> str:
    get = services.settings_repo.get
    keys = (
        "knowledge_embedding_backend",
        "knowledge_embedding_model",
        "knowledge_embedding_provider_id",
        "knowledge_chunk_size",
        "knowledge_chunk_overlap",
    )
    values = [get(key) for key in keys]
    provider_id = str(values[2] or "")
    if provider_id.isdigit():
        provider = services.provider_repo.get(int(provider_id))
        values.append(getattr(provider, "base_url", None))
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def search(
    services: Any, user: int, source: str, query_vector: list[float], k: int
) -> tuple[list[Any], tuple[str, int, str]] | None:
    try:
        with _source_lock(services, user, source):
            return _search_locked(services, user, source, query_vector, k)
    except Exception:
        log.warning("private Feishu source lock failed closed", exc_info=True)
        return None


def _search_locked(
    services: Any, user: int, source: str, query_vector: list[float], k: int
) -> tuple[list[Any], tuple[str, int, str]] | None:
    service = FeishuKbService(services)
    try:
        if not _valid(service, user, source):
            return None
        content = service.read(user, source)
        digest = hashlib.sha256(content.encode()).hexdigest()
        row = service.repo.source(source, user)
        if not row:
            return None
        generation = int(row["retrieval_generation"])
        if not _valid(service, user, source, digest, generation):
            return None
        config_version = version(services)
        collection_id = collection(user, source, digest, config_version, generation)
        # Commit the permanent cleanup record before embedding or any index write.
        with services.db.transaction() as c:
            c.execute(
                "INSERT INTO feishu_kb_private_index_versions (collection_id,source_id,user_id,digest,config_version,generation,ready) VALUES (?,?,?,?,?,?,0) ON CONFLICT(collection_id) DO NOTHING",
                (collection_id, source, user, digest, config_version, generation),
            )
            mapping = c.execute("SELECT ready FROM feishu_kb_private_index_versions WHERE collection_id=?", (collection_id,)).fetchone()
        index = open_knowledge_index(services, collection_id)
        if not mapping["ready"]:
            assert_knowledge_usable(services.settings_repo.get, services.provider_repo)
            knobs = get_advanced_settings(services.settings_repo.get)
            chunks = chunk_text(content, size=knobs["chunk_size"], overlap=knobs["chunk_overlap"])
            if not _valid(service, user, source, digest, generation) or version(services) != config_version:
                return None
            vectors = embed_knowledge_texts(services, chunks) if chunks else []
            if not _valid(service, user, source, digest, generation) or version(services) != config_version:
                return None
            index.replace_doc_chunks(source, chunks, vectors)
            if not _valid(service, user, source, digest, generation) or version(services) != config_version:
                # Only this writer's immutable collection can be touched here.
                index.delete_doc(source)
                return None
            with services.db.transaction() as c:
                c.execute("UPDATE feishu_kb_private_index_versions SET ready=1 WHERE collection_id=?", (collection_id,))
        hits = index.search(query_vector, k=k)
        if (
            not _valid(service, user, source, digest, generation)
            or version(services) != config_version
        ):
            return None
        return hits, (digest, generation, config_version)
    except Exception:
        log.warning("private Feishu retrieval failed closed", exc_info=True)
        return None


def fence(services: Any, user: int, source: str, token: tuple[str, int, str]) -> bool:
    digest, generation, config_version = token
    if not _valid(FeishuKbService(services), user, source, digest, generation):
        return False
    if version(services) != config_version:
        return False
    with services.db.connect() as c:
        row = c.execute(
            "SELECT 1 FROM feishu_kb_private_index_versions WHERE collection_id=? AND ready=1",
            (collection(user, source, digest, config_version, generation),),
        ).fetchone()
    return row is not None
