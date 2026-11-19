"""PostgreSQL acceptance tests; OCTOP_TEST_DATABASE_URL must name a dedicated local DB.

Each test creates and removes only its own schema. No SQLite fallback is used.
"""

from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from octop.config import OctopConfig
from octop.infra.auth.sso.crypto import encrypt_secret
from octop.infra.auth.sso.service import SsoService
from octop.infra.auth.youli.reauth import FeishuErpReauth
from octop.infra.db import migrate
from octop.infra.db.pool import PostgresPool
from octop.infra.db.repos._base import (
    PrivateThreadBindingError,
    PrivateThreadForkError,
    insert_returning_id,
)
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.sessions import SessionRepo
from octop.infra.db.repos.threads import ThreadRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import build_shared_services
from octop.infra.users.manager import UserManager
from octop.infra.utils.paths import PathLayout
from tests.support.postgresql import requires_postgresql

pytestmark = [requires_postgresql, pytest.mark.postgresql]


@pytest.fixture
def pg(tmp_path: Path):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    dsn = os.environ["OCTOP_TEST_DATABASE_URL"]
    params = conninfo_to_dict(dsn)
    if (
        params.get("host") not in {"127.0.0.1", "localhost", "::1"}
        or (params.get("hostaddr") and params["hostaddr"] not in {"127.0.0.1", "::1"})
        or not params.get("dbname")
        or "options" in params
        or "service" in params
    ):
        pytest.fail("OCTOP_TEST_DATABASE_URL must explicitly name a dedicated local PostgreSQL DB")
    schema = "feishu_test_" + uuid.uuid4().hex
    local_address = params.get("hostaddr") or (
        "127.0.0.1" if params["host"] == "localhost" else params["host"]
    )
    dsn = make_conninfo(dsn, hostaddr=local_address)
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {} ").format(sql.Identifier(schema)))
        scoped = make_conninfo(dsn, options=f"-csearch_path={schema}")
        first = PostgresPool(scoped, min_size=1, max_size=2)
        second = PostgresPool(scoped, min_size=1, max_size=2)
        try:
            with first.connect() as a, second.connect() as b:
                assert (
                    a.execute("SELECT pg_backend_pid()").fetchone()[0]
                    != b.execute("SELECT pg_backend_pid()").fetchone()[0]
                )
            yield first, second, tmp_path
        finally:
            first.close()
            second.close()
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_upgrade_real_v26_schema_is_idempotent(pg, monkeypatch):
    db, _, _ = pg
    discover = migrate._discover
    with monkeypatch.context() as patch:
        patch.setattr(
            migrate,
            "_discover",
            lambda dialect="sqlite": [item for item in discover(dialect) if item[0] <= 26],
        )
        migrate.run_migrations(db)
    assert migrate._current_version(db) == 26
    with db.transaction() as conn:
        uid = insert_returning_id(
            conn,
            "INSERT INTO users(username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
            ("old-owner", "hash", "user", 0),
        )
    AgentRepo(db).create(agent_id="old-agent", user_id=uid, name="Old agent")
    # At v26 these objects really do not exist; this is not a lowered watermark.
    assert not migrate._table_exists(db, "feishu_kb_source")
    assert "private_knowledge_protected" not in migrate._table_columns(db, "threads")
    migrate.run_migrations(db)
    migrate.run_migrations(db)
    assert migrate._current_version(db) == max(v for v, _ in discover("postgresql"))
    assert UserRepo(db).get(uid).username == "old-owner"
    assert AgentRepo(db).get("old-agent") is not None
    assert migrate._table_exists(db, "feishu_kb_private_index_versions")
    assert "had_independent_identity" in migrate._table_columns(db, "youli_identity_binding")
    assert "unlinked" in migrate._table_columns(db, "youli_identity_binding")
    assert "private_knowledge_protected" in migrate._table_columns(db, "threads")


def test_protection_races_contribution_binding_and_fork(pg):
    db, other, _ = pg
    migrate.run_migrations(db)
    uid = UserRepo(db).create(username="owner", password_hash="hash", role="user")
    AgentRepo(db).create(agent_id="agent", user_id=uid, name="Agent")
    threads, remote = ThreadRepo(db), ThreadRepo(other)
    sessions = SessionRepo(other)
    for operation in ("contribute", "bind", "fork"):
        tid = f"root-{operation}"
        threads.insert(
            thread_id=tid,
            agent_id="agent",
            user_id=uid,
            channel_type="dashboard",
            session_key=f"agent:dashboard:{uid}:dm",
        )
        gate = Barrier(2)

        def protect(gate=gate, tid=tid):
            gate.wait(timeout=5)
            return threads.protect_private_knowledge(tid, uid)

        def competing(gate=gate, tid=tid, operation=operation):
            gate.wait(timeout=5)
            if operation == "contribute":
                return remote.set_contributed(tid, True)
            if operation == "bind":
                try:
                    sessions.upsert(
                        session_key=f"agent:feishu:{uid}:dm",
                        agent_id="agent",
                        user_id=uid,
                        channel_type="feishu",
                        chat_type="dm",
                        thread_id=tid,
                    )
                    return True
                except PrivateThreadBindingError:
                    return False
            try:
                remote.insert(
                    thread_id="fork",
                    agent_id="agent",
                    user_id=uid,
                    channel_type="dashboard",
                    session_key="fork",
                    fork_source_id=tid,
                )
                return True
            except PrivateThreadForkError:
                return False

        with ThreadPoolExecutor(max_workers=2) as executor:
            a, b = executor.submit(protect), executor.submit(competing)
            protected, competed = a.result(timeout=10), b.result(timeout=10)
        if operation != "fork":
            assert protected != competed
        else:
            # Forking before protection can succeed; protection then remains effective.
            assert protected
            if not competed:
                assert remote.get("fork") is None
            with pytest.raises(PrivateThreadForkError):
                remote.insert(
                    thread_id="late-fork",
                    agent_id="agent",
                    user_id=uid,
                    channel_type="dashboard",
                    session_key="fork",
                    fork_source_id=tid,
                )
        row = threads.get(tid)
        assert row is not None and row.private_knowledge_protected == protected
        assert not threads.protect_private_knowledge(tid, uid + 10000)


def test_employee_login_and_delegation_compete_for_one_owner(pg, monkeypatch):
    db, other, home = pg
    migrate.run_migrations(db)
    paths = PathLayout(home / "octop")
    paths.ensure_root()
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    uid = services.user_repo.create(username="owner", password_hash="hash", role="user")
    feishu = services.sso_repo.upsert_by_kind(
        "feishu",
        enabled=True,
        display_name="Feishu",
        issuer="",
        client_id="app",
        client_secret_enc=encrypt_secret(services.secret_repo, "test-secret"),
        scopes="",
        dashboard_origin="https://example.invalid",
        extra={},
    )
    youli = services.sso_repo.upsert_by_kind(
        "youli",
        enabled=True,
        display_name="ERP",
        issuer="",
        client_id="erp",
        client_secret_enc=None,
        scopes="",
        dashboard_origin=None,
        extra={},
    )
    subject = 'feishu-v1:["feishu","app","tenant","person"]'
    services.user_repo.upsert_sso_identity(uid, provider_id=feishu.id, subject=subject)

    def handler(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"access_token": "fake"})
        return httpx.Response(
            200, json={"code": 0, "data": {"tenant_key": "tenant", "union_id": "person"}}
        )

    sso = SsoService(services, UserManager(services))
    monkeypatch.setattr(
        sso, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    reauth = FeishuErpReauth(services, sso)
    state = parse_qs(urlparse(reauth.start(uid, "session", feishu.id)).query)["state"][0]
    claim = reauth.claim(uid, "session", reauth.callback(uid, "session", state, "code"))
    gate = Barrier(2)
    expiry = int(time.time()) + 3600

    def bind():
        gate.wait(timeout=5)
        try:
            services.youli_identity_binding_repo.bind(
                user_id=uid,
                provider_id=youli.id,
                subject="youli:21:31",
                feishu_claim=claim,
                feishu_subject=subject,
                sid="delegation",
                account_user_id=11,
                account_username="person",
                company_id=21,
                employee_id=31,
                industry=1,
                token_blob=b"encrypted",
                token_expires_at=expiry,
                abs_expires_at=expiry,
            )
            return True
        except ValueError:
            return False

    def login():
        gate.wait(timeout=5)
        try:
            UserRepo(other).create(
                username="erp-login",
                role="user",
                sso_provider_id=youli.id,
                sso_subject="youli:21:31",
            )
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        a, b = executor.submit(bind), executor.submit(login)
        bound, logged_in = a.result(timeout=10), b.result(timeout=10)
    assert bound != logged_in
    assert (services.youli_identity_binding_repo.get(uid) is not None) == bound
    assert (UserRepo(other).get_by_username("erp-login") is not None) == logged_in
    assert len(services.youli_session_repo.list_active_for_user(uid)) == int(bound)


def test_private_index_publish_then_revoke_during_rebuild(pg, monkeypatch):
    """A revoked grant cannot publish an in-flight immutable pgvector collection."""
    from dataclasses import replace
    from threading import Event

    from psycopg.conninfo import make_conninfo

    from octop.config import DatabaseConfig
    from octop.infra.feishu_kb import private_index
    from octop.infra.feishu_kb.client import FeishuClient
    from octop.infra.feishu_kb.service import FeishuKbService
    from octop.infra.knowledge.plane import open_knowledge_index

    db, other, home = pg
    migrate.run_migrations(db)
    with db.connect() as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        assert conn.execute("SELECT 1 FROM pg_type WHERE typname='vector'").fetchone(), (
            "dedicated PostgreSQL must have pgvector installed"
        )
        # The production index's legacy adoption mutates these schemas if present.
        assert not conn.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname IN ('octop_knowledge','octop_skill_packages')"
        ).fetchone(), "dedicated database must not contain legacy storage schemas"
    index_dsn = make_conninfo(db._pool.conninfo, options=f"-csearch_path={schema},public")
    config = replace(OctopConfig(), database=DatabaseConfig(driver="postgresql", url=index_dsn))
    paths = PathLayout(home / "octop")
    paths.ensure_root()
    services = build_shared_services(db=db, paths=paths, config=config)
    uid = services.user_repo.create(username="owner", password_hash="hash", role="user")
    provider = services.sso_repo.upsert_by_kind(
        "feishu",
        enabled=True,
        display_name="Feishu",
        issuer="",
        client_id="app",
        client_secret_enc=encrypt_secret(services.secret_repo, "test-secret"),
        scopes="",
        dashboard_origin="https://example.invalid",
        extra={},
    )
    subject = 'feishu-v1:["feishu","app","tenant","person"]'
    services.user_repo.upsert_sso_identity(uid, provider_id=provider.id, subject=subject)
    upstream = {"content": "first private content"}

    def handler(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200, json={"access_token": "fake", "refresh_token": "fake", "expires_in": 3600}
            )
        if request.url.path.endswith("/user_info"):
            return httpx.Response(
                200, json={"code": 0, "data": {"tenant_key": "tenant", "union_id": "person"}}
            )
        return httpx.Response(200, json={"code": 0, "data": {"content": upstream["content"]}})

    def client(_self, _provider):
        return FeishuClient(
            httpx.Client(transport=httpx.MockTransport(handler)),
            client_id="app",
            client_secret="test-secret",
        )

    monkeypatch.setattr(FeishuKbService, "_client", client)
    embedding_provider = services.provider_repo.create(
        name="test-embedding",
        kind="openai",
        base_url="https://example.invalid",
        api_key="fake",
        models_json='[{"id":"test-embedding","embedding":true}]',
    )
    # These settings exist solely in the test schema; production defaults remain off.
    for key, value in {
        "knowledge_bases_enabled": "true",
        "knowledge_embedding_backend": "remote",
        "knowledge_embedding_model": "test-embedding",
        "knowledge_embedding_provider_id": str(embedding_provider),
    }.items():
        services.settings_repo.set(key, value)
    service = FeishuKbService(services)
    state = parse_qs(urlparse(service.authorize(uid, provider.id, "session")).query)["state"][0]
    service.callback(uid, "session", state, "code")
    source = service.create(uid, provider.id, "doc")
    assert service.repo.source(source, uid)["enabled"] == 0
    assert service.repo.source(source, uid)["retrieval_enabled"] == 0
    service.configure(uid, source, True, 900, True)
    started, release = Event(), Event()

    def embed(_services, texts):
        if texts == ["second private content"]:
            started.set()
            assert release.wait(10)
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(private_index, "embed_knowledge_texts", embed)
    try:
        first = private_index.search(services, uid, source, [1.0, 0.0], 4)
        assert first is not None
        assert [hit.text for hit in first[0]] == ["first private content"]
        digest, generation, version = first[1]
        old_collection = private_index.collection(uid, source, digest, version, generation)
        assert open_knowledge_index(services, old_collection).list_doc_chunks(source) == [
            (0, "first private content")
        ]
        upstream["content"] = "second private content"
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(private_index.search, services, uid, source, [1.0, 0.0], 4)
            try:
                assert started.wait(10)
                from octop.infra.db.repos.feishu_kb import FeishuKbRepo

                FeishuKbRepo(other).revoke(uid, provider.id)
            finally:
                release.set()
            assert pending.result(timeout=10) is None
        assert not private_index.fence(services, uid, source, first[1])
        with db.connect() as conn:
            versions = conn.execute(
                "SELECT collection_id,ready FROM feishu_kb_private_index_versions WHERE source_id=?",
                (source,),
            ).fetchall()
        assert len(versions) == 2
        assert sorted(int(row["ready"]) for row in versions) == [0, 1]
        new_collection = next(row["collection_id"] for row in versions if not row["ready"])
        assert open_knowledge_index(services, new_collection).list_doc_chunks(source) == []
    finally:
        release.set()
        with db.connect() as conn:
            versions = conn.execute(
                "SELECT collection_id FROM feishu_kb_private_index_versions WHERE source_id=?",
                (source,),
            ).fetchall()
            # Only this test's immutable collection IDs are deleted from the shared index table.
            if conn.execute("SELECT to_regclass('public.knowledge_chunks')").fetchone()[0]:
                for row in versions:
                    conn.execute(
                        "DELETE FROM public.knowledge_chunks WHERE kb_id=?", (row["collection_id"],)
                    )
