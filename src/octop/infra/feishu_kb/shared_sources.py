"""Publish explicitly approved Feishu directory snapshots into a knowledge base."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from octop.infra.auth.sso.crypto import decrypt_secret, encrypt_secret
from octop.infra.db.repos.shared_feishu import SharedFeishuRepo
from octop.infra.feishu_kb.client import FeishuError, Grant, authorize_url
from octop.infra.feishu_kb.directories import DIRECTORY_SCOPES, DirectoryKind, FeishuDirectoryClient
from octop.infra.feishu_kb.service import FeishuKbService, _hash
from octop.infra.knowledge.blobs import KnowledgeBlobs
from octop.infra.knowledge.gate import assert_knowledge_usable
from octop.infra.knowledge.plane import open_knowledge_index
from octop.infra.knowledge.service import KnowledgeService
from octop.infra.users.groups import RESOURCE_KNOWLEDGE_BASE, GroupAccess
from octop.infra.utils.ulid import new_ulid

logger = logging.getLogger(__name__)
MAX_DOCUMENT_BYTES = 2_000_000
MAX_BATCH_BYTES = 16_000_000
LEASE_SECONDS = 420
MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 43_200


@dataclass(frozen=True)
class Scope:
    kind: DirectoryKind
    root: str
    title: str
    space: str | None = None
    include_children: bool = True


class SharedFeishuService:
    def __init__(
        self,
        services: Any,
        *,
        clock: Callable[[], float] = time.time,
        client_factory: Callable[[Any], FeishuDirectoryClient] | None = None,
    ) -> None:
        self.services = services
        self.repo = SharedFeishuRepo(services.db)
        self.knowledge = KnowledgeService(services)
        self.providers = FeishuKbService(services)
        self.clock = clock
        self.client_factory = client_factory

    def _client(self, provider: Any) -> FeishuDirectoryClient:
        if self.client_factory:
            return self.client_factory(provider)
        secret = decrypt_secret(self.services.secret_repo, provider.client_secret_enc)
        return FeishuDirectoryClient(
            httpx.Client(timeout=10.0),
            client_id=provider.client_id,
            client_secret=secret,
        )

    def providers_list(self) -> list[dict[str, Any]]:
        options = []
        for row in self.services.sso_repo.list():
            try:
                provider = self.providers._provider(row.id)
            except FeishuError:
                continue
            if provider.dashboard_origin:
                options.append({"id": provider.id, "name": provider.display_name})
        return options

    def connections(self, owner: int) -> list[dict[str, Any]]:
        return [
            {
                "id": row["id"],
                "label": row["label"],
                "provider_id": row["provider_id"],
                "tenant": json.loads(row["subject"].removeprefix("feishu-v1:"))[2],
                "connected": bool(row["connected"]),
            }
            for row in self.repo.connections(owner)
        ]

    def authorize(self, owner: int, provider_id: int, session: str, label: str) -> str:
        provider = self.providers._provider(provider_id)
        if not provider.dashboard_origin or not label.strip():
            raise FeishuError("provider_unavailable")
        redirect = provider.dashboard_origin.rstrip("/") + "/feishu-kb/callback"
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        now = int(self.clock())
        self.repo.store_state(
            (
                _hash(state),
                owner,
                provider_id,
                _hash(session),
                encrypt_secret(self.services.secret_repo, verifier),
                redirect,
                label.strip(),
                provider.client_id,
                now + 300,
            ),
            now,
        )
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        return authorize_url(
            client_id=provider.client_id,
            redirect_uri=redirect,
            state=state,
            challenge=challenge,
            scopes=DIRECTORY_SCOPES,
        )

    def callback(self, owner: int, session: str, state: str, code: str) -> None:
        row = self.repo.consume_state(_hash(state), owner, _hash(session), int(self.clock()))
        if row is None:
            raise FeishuError("invalid_state")
        provider = self.providers._provider(row["provider_id"])
        if provider.client_id != row["client_id"]:
            raise FeishuError("provider_unavailable")
        client = self._client(provider)
        try:
            grant = client.exchange(
                code=code,
                verifier=decrypt_secret(self.services.secret_repo, bytes(row["verifier_blob"])),
                redirect_uri=row["redirect_uri"],
            )
            subject = client.subject(grant.access_token)
        finally:
            client.close()
        if not self.providers._subject_matches(subject, provider.client_id):
            raise FeishuError("invalid_identity")
        self.repo.connect(
            (
                new_ulid(),
                owner,
                provider.id,
                subject,
                row["label"],
                self._encrypt(grant),
                int(self.clock()) + grant.expires_in,
            )
        )

    def _encrypt(self, grant: Grant) -> bytes:
        return encrypt_secret(
            self.services.secret_repo,
            json.dumps({"access": grant.access_token, "refresh": grant.refresh_token}),
        )

    @contextmanager
    def client(self, owner: int, connection_id: str) -> Iterator[tuple[Any, str]]:
        row = self.repo.connection(owner, connection_id)
        if not row or not row["token_blob"] or row["refreshing"]:
            raise FeishuError("connection_unavailable")
        provider = self.providers._provider(row["provider_id"])
        if not self.providers._subject_matches(row["subject"], provider.client_id):
            raise FeishuError("connection_unavailable")
        old = bytes(row["token_blob"])
        credentials = json.loads(decrypt_secret(self.services.secret_repo, old))
        client = self._client(provider)
        try:
            if row["expires_at"] <= int(self.clock()) + 60:
                if not self.repo.claim_refresh(connection_id, old):
                    raise FeishuError("connection_unavailable")
                grant = client.refresh(credentials["refresh"])
                if not self.repo.finish_refresh(
                    connection_id, old, self._encrypt(grant), int(self.clock()) + grant.expires_in
                ):
                    raise FeishuError("connection_unavailable")
                yield client, grant.access_token
            else:
                yield client, credentials["access"]
        finally:
            client.close()

    def browse(
        self,
        owner: int,
        connection_id: str,
        kind: str,
        parent: str | None,
        space: str | None,
        page: str | None,
    ) -> dict[str, Any]:
        with self.client(owner, connection_id) as (client, access):
            if kind == "wiki_spaces":
                result = client.wiki_spaces(access, page)
            elif kind == "wiki_nodes" and space:
                result = client.wiki_nodes(access, space, parent, page)
            elif kind == "drive_folders":
                result = client.drive_files(access, parent, page, folders_only=True)
            else:
                raise FeishuError("invalid_directory_id")
        return {
            "items": [asdict(item) for item in result.items if item.has_children],
            "next_page": result.next_page,
        }

    def _approval(self, owner: int, kb_id: str, connection_id: str, scope: Scope) -> dict[str, Any]:
        if not scope.title:
            raise FeishuError("invalid_directory_name")
        base = self.knowledge.get_writable_base(kb_id, actor_user_id=owner)
        assert_knowledge_usable(self.services.settings_repo.get, self.services.provider_repo)
        with self.client(owner, connection_id) as (client, access):
            docs, skipped = client.discover(
                access, scope.kind, scope.root, scope.space, scope.include_children
            )
        visibility = GroupAccess(self.services.repos).visibility(
            resource_type=RESOURCE_KNOWLEDGE_BASE,
            resource_key=kb_id,
            shared=base.shared,
        )
        group_names = (
            {
                group.user_group_id: group.name
                for group in self.services.repos.user_group_repo.list_all()
            }
            if visibility["group_ids"]
            else {}
        )
        existing_raw = self.repo.approved_document_ids(
            kb_id, connection_id, scope.kind, scope.space or "", scope.root
        )
        existing_ids: set[str] | None = (
            set(json.loads(existing_raw)) if existing_raw is not None else None
        )
        approved: list[dict[str, Any]] = [
            {
                **asdict(doc),
                "is_new": existing_ids is not None and doc.id not in existing_ids,
            }
            for doc in docs
        ]
        if existing_ids is not None:
            approved.sort(key=lambda item: not bool(item["is_new"]))
        fingerprint = _hash(
            json.dumps(
                [
                    owner,
                    kb_id,
                    connection_id,
                    asdict(scope),
                    visibility,
                    sorted(doc.id for doc in docs),
                ]
            )
        )
        return {
            "documents": approved,
            "skipped": skipped,
            "existing_source": existing_ids is not None,
            "new_count": sum(1 for item in approved if item["is_new"]),
            "visibility": {
                **visibility,
                "group_names": [
                    group_names.get(group_id, group_id) for group_id in visibility["group_ids"]
                ],
            },
            "fingerprint": fingerprint,
        }

    def preview(self, owner: int, kb_id: str, connection_id: str, scope: Scope) -> dict[str, Any]:
        return self._approval(owner, kb_id, connection_id, scope)

    def approve(
        self,
        owner: int,
        kb_id: str,
        connection_id: str,
        scope: Scope,
        fingerprint: str,
        *,
        schedule: str,
        auto_add: bool,
        interval_minutes: int | None = None,
    ) -> str:
        if schedule == "interval":
            if (
                isinstance(interval_minutes, bool)
                or not isinstance(interval_minutes, int)
                or interval_minutes < MIN_INTERVAL_MINUTES
                or interval_minutes > MAX_INTERVAL_MINUTES
            ):
                raise FeishuError("invalid_schedule")
            db_schedule = "daily"
            db_interval = interval_minutes
        elif schedule in {"manual", "daily"}:
            db_schedule = schedule
            db_interval = 0
        else:
            raise FeishuError("invalid_schedule")
        approval = self._approval(owner, kb_id, connection_id, scope)
        if not secrets.compare_digest(approval["fingerprint"], fingerprint):
            raise FeishuError("review_changed")
        now = int(self.clock())
        stored = self.repo.save_source(
            (
                new_ulid(),
                kb_id,
                connection_id,
                owner,
                scope.kind,
                scope.root,
                scope.space or "",
                scope.title,
                int(scope.include_children),
                int(auto_add),
                db_schedule,
                db_interval,
                now,
                json.dumps([doc["id"] for doc in approval["documents"]]),
                json.dumps(approval["visibility"]),
                now,
                connection_id,
                owner,
            )
        )
        if stored is None:
            raise FeishuError("connection_unavailable")
        return str(stored["id"])

    def scan(self, limit: int = 2) -> list[tuple[str, str]]:
        indexed = []
        for row in self.repo.due(int(self.clock()), limit):
            token = secrets.token_urlsafe(20)
            now = int(self.clock())
            source = self.repo.claim(row["id"], token, now, now + LEASE_SECONDS)
            if source is None:
                continue
            try:
                indexed.extend(self._sync(source, token))
            except FeishuError as exc:
                self.repo.fail(source, token, int(self.clock()), exc.reason)
            except (RuntimeError, ValueError, PermissionError, LookupError):
                self.repo.fail(source, token, int(self.clock()), "knowledge_unavailable")
            except Exception:
                logger.warning(
                    "Shared Feishu synchronization failed for %s", source["id"], exc_info=True
                )
                self.repo.fail(source, token, int(self.clock()), "sync_failed")
        self._cleanup()
        return indexed

    def _sync(self, source: Any, token: str) -> list[tuple[str, str]]:
        owner, kb_id = source["approved_by"], source["kb_id"]
        user = self.services.user_repo.get(owner)
        if (
            user is None
            or user.disabled
            or (user.role != "admin" and "knowledge_bases" not in user.permissions)
        ):
            raise FeishuError("owner_unavailable")
        self.knowledge.get_writable_base(kb_id, actor_user_id=owner)
        assert_knowledge_usable(self.services.settings_repo.get, self.services.provider_repo)
        connection_id = source["connection_id"]
        connection = self.repo.connection(owner, connection_id)
        if connection is None:
            raise FeishuError("connection_unavailable")
        allowed = set(json.loads(source["approved_document_ids"]))
        published = self.repo.published(source["id"])
        blobs = KnowledgeBlobs.from_services(self.services)
        inserted = []
        keep: set[str] = set()
        pending = 0
        total = 0
        with self.client(owner, connection_id) as (client, access):
            docs, skipped = client.discover(
                access,
                source["scope_kind"],
                source["root_token"],
                source["space_id"] or None,
                bool(source["include_children"]),
            )
            for doc in docs:
                if doc.id not in allowed and not source["auto_add"]:
                    pending += 1
                    continue
                now = int(self.clock())
                if now >= source["lease_until"]:
                    raise FeishuError("sync_timed_out")
                try:
                    raw = client.raw_content(doc.id, access).encode("utf-8")
                except FeishuError as exc:
                    if exc.reason not in {"missing", "forbidden"}:
                        raise
                    skipped += 1
                    continue
                total += len(raw)
                if total > MAX_BATCH_BYTES:
                    raise FeishuError("directory_content_too_large")
                if (
                    len(raw) > min(MAX_DOCUMENT_BYTES, self.knowledge._max_document_bytes())
                    or not raw.strip()
                ):
                    skipped += 1
                    continue
                digest = hashlib.sha256(raw).hexdigest()
                origin = json.dumps(
                    {
                        "connection": connection["label"],
                        "scope": source["title"],
                        "path": list(doc.path),
                        "title": doc.title,
                    },
                    ensure_ascii=False,
                )
                old = published.get(doc.id)
                if (
                    old is not None
                    and old["content_hash"] == digest
                    and old["feishu_origin"] == origin
                ):
                    keep.add(old["document_id"])
                    continue
                document_id = new_ulid()
                safe_title = (
                    re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", doc.title).strip(" .")[:70] or "Feishu"
                )
                filename = f"{safe_title} · {source['id'][-8:]}-{_hash(doc.id)[:8]}.txt"
                self.repo.retain_blob(document_id, kb_id, filename, source["lease_until"] + 3600)
                blobs.write(kb_id, document_id, filename, raw)
                inserted.append(
                    (
                        document_id,
                        kb_id,
                        filename,
                        filename,
                        len(raw),
                        digest,
                        now,
                        now,
                        source["id"],
                        doc.id,
                        origin,
                    )
                )
        if self.repo.publish(
            source=source,
            token=token,
            now=int(self.clock()),
            insert=inserted,
            keep_ids=keep,
            pending=pending,
            skipped=skipped,
            approved_document_ids=json.dumps(sorted(allowed)),
        ):
            return [(kb_id, row[0]) for row in inserted]
        return []

    def _cleanup(self) -> None:
        blobs = KnowledgeBlobs.from_services(self.services)
        for row in self.repo.cleanup(int(self.clock()), 25):
            try:
                open_knowledge_index(self.services, row["kb_id"]).delete_doc(row["document_id"])
                blobs.delete(row["kb_id"], row["document_id"], row["filename"])
            except Exception:
                logger.warning("Shared Feishu cleanup will retry", exc_info=True)
            else:
                self.repo.finish_cleanup(row["document_id"])
