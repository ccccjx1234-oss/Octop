"""Private, read-through Feishu document staging; no global KB integration."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable
from typing import Any

import httpx

from octop.infra.auth.sso.crypto import decrypt_secret, encrypt_secret
from octop.infra.auth.sso.public_base import callback_origin
from octop.infra.db.repos.feishu_kb import FeishuKbRepo
from octop.infra.feishu_kb.client import FeishuClient, FeishuError, Grant, authorize_url


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class FeishuKbService:
    def __init__(
        self,
        services: Any,
        client_factory: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.services = services
        self.repo = FeishuKbRepo(services.db)
        self.clock = clock
        self.client_factory = client_factory

    def status(self, user: int) -> list[dict[str, Any]]:
        identities = []
        for row in self.repo.identities(user):
            try:
                provider = self._provider(int(row["provider_id"]))
            except FeishuError:
                continue
            subject = row["subject"]
            if not self._subject_matches(subject, provider.client_id):
                continue
            identity = json.loads(subject.removeprefix("feishu-v1:"))
            grant = self.repo.grant(user, provider.id)
            identities.append(
                {
                    "provider_id": provider.id,
                    "provider_name": getattr(provider, "display_name", "Feishu"),
                    "tenant": identity[2],
                    "identity": identity[3],
                    "consent": "authorized" if grant and not grant["refreshing"] else "required",
                    "requested_scopes": ["docx:document:readonly", "offline_access"],
                }
            )
        return identities

    def _provider(self, provider_id: int) -> Any:
        provider = self.services.sso_repo.get_by_id(provider_id)
        if (
            not provider
            or provider.kind != "feishu"
            or not provider.enabled
            or not provider.client_secret_enc
            or not provider.client_id
            or provider.extra.get("region") == "lark"
        ):
            raise FeishuError("provider_unavailable")
        return provider

    def _client(self, provider: Any) -> Any:
        if self.client_factory:
            return self.client_factory(provider)
        secret = decrypt_secret(self.services.secret_repo, provider.client_secret_enc)
        return FeishuClient(
            httpx.Client(timeout=10.0), client_id=provider.client_id, client_secret=secret
        )

    def authorize(
        self, user: int, provider_id: int, session: str, public_base: str = ""
    ) -> str:
        provider = self._provider(provider_id)
        subject = self.repo.linked_subject(user, provider_id)
        origin = callback_origin(provider.dashboard_origin, public_base)
        if (
            not subject
            or not self._subject_matches(subject, provider.client_id)
            or not origin
        ):
            raise FeishuError("identity_unavailable")
        redirect = origin + "/feishu-kb/callback"
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        now = int(self.clock())
        created = self.repo.state_create(
            (
                _hash(state),
                user,
                provider_id,
                _hash(session),
                encrypt_secret(self.services.secret_repo, verifier),
                redirect,
                subject,
                now + 300,
            ),
            now,
        )
        if not created:
            raise FeishuError("identity_unavailable")
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        return authorize_url(
            client_id=provider.client_id, redirect_uri=redirect, state=state, challenge=challenge
        )

    def callback(self, user: int, session: str, state: str, code: str) -> None:
        key = _hash(state)
        row = self.repo.state_consume(key, user, _hash(session), int(self.clock()))
        if row is None:
            raise FeishuError("invalid_state")
        provider = self._provider(int(row["provider_id"]))
        client = self._client(provider)
        try:
            grant = client.exchange(
                code=code,
                verifier=decrypt_secret(self.services.secret_repo, bytes(row["verifier_blob"])),
                redirect_uri=row["redirect_uri"],
            )
            subject = client.subject(grant.access_token)
        finally:
            self._close(client)
        if subject != row["subject"]:
            raise FeishuError("identity_mismatch")
        blob = self._encrypt_grant(grant)
        now = int(self.clock())
        if not self.repo.grant_finish(
            key, user, provider.id, subject, blob, now + grant.expires_in, now
        ):
            raise FeishuError("invalid_state")

    def _encrypt_grant(self, grant: Grant) -> bytes:
        return encrypt_secret(
            self.services.secret_repo,
            json.dumps({"access": grant.access_token, "refresh": grant.refresh_token}),
        )

    @staticmethod
    def _subject_matches(subject: str, client_id: str) -> bool:
        try:
            identity = json.loads(subject.removeprefix("feishu-v1:"))
            return (
                subject.startswith("feishu-v1:")
                and isinstance(identity, list)
                and len(identity) == 4
                and identity[0] == "feishu"
                and identity[1] == client_id
                and all(isinstance(v, str) and v for v in identity[2:])
            )
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _close(client: Any) -> None:
        close = getattr(client, "close", None)
        if close is not None:
            close()

    def _access(self, user: int, provider_id: int) -> tuple[Any, str]:
        provider = self._provider(provider_id)
        row = self.repo.grant(user, provider_id)
        if (
            row is None
            or row["refreshing"]
            or not self._subject_matches(row["subject"], provider.client_id)
        ):
            raise FeishuError("grant_unavailable")
        old = bytes(row["token_blob"])
        data = json.loads(decrypt_secret(self.services.secret_repo, old))
        client = self._client(provider)
        if int(row["expires_at"]) <= int(self.clock()) + 60:
            if not self.repo.claim_refresh(user, provider_id, old):
                raise FeishuError("grant_unavailable")
            # Claim is durable before the one-shot upstream call. A crash or uncertain
            # response requires reauthorization; never reuse a possibly spent token.
            try:
                grant = client.refresh(data["refresh"])
            except Exception:
                self._close(client)
                raise
            blob = self._encrypt_grant(grant)
            if not self.repo.finish_refresh(
                user, provider_id, old, blob, int(self.clock()) + grant.expires_in
            ):
                raise FeishuError("grant_unavailable")
            return client, grant.access_token
        return client, data["access"]

    def create(self, user: int, provider: int, document: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", document):
            raise FeishuError("invalid_document_id")
        if self.repo.grant(user, provider) is None:
            raise FeishuError("grant_unavailable")
        source = secrets.token_urlsafe(18)
        if not self.repo.create_source(source, user, provider, document):
            raise FeishuError("source_limit")
        return source

    def configure(
        self, user: int, source: str, enabled: bool, interval: int, retrieval: bool | None = None
    ) -> None:
        if not 900 <= interval <= 2592000:
            raise FeishuError("invalid_interval")
        if not self.repo.configure(source, user, enabled, interval, retrieval):
            raise FeishuError("source_unavailable")
        if not enabled or retrieval is False:
            from octop.infra.feishu_kb.private_index import cleanup

            cleanup(self.services)

    def sync(self, user: int, source: str) -> str:
        token = secrets.token_urlsafe(24)
        now = int(self.clock())
        if not self.repo.claim(source, user, token, now):
            raise FeishuError("source_unavailable")
        row = self.repo.source(source, user)
        if row is None:
            raise FeishuError("source_unavailable")
        try:
            client, access = self._access(user, int(row["provider_id"]))
            try:
                content = client.raw_content(row["document_id"], access)
            finally:
                self._close(client)
            if len(content.encode()) > 2_000_000:
                raise FeishuError("document_too_large")
            digest = _hash(content)
            status = "unchanged" if row["digest"] == digest else "updated"
        except FeishuError as exc:
            # Any failed read removes the local copy, including transient failures.
            self.repo.fail(source, user, token, int(self.clock()), exc.reason)
            from octop.infra.feishu_kb.private_index import invalidate

            invalidate(self.services, user, source)
            raise
        if not self.repo.finish(source, user, token, int(self.clock()), status, digest, content):
            raise FeishuError("source_unavailable")
        return status

    def read(self, user: int, source: str) -> str:
        # Synchronize first: every response requires a successful live Feishu read.
        self.sync(user, source)
        row = self.repo.source(source, user)
        grant = self.repo.grant(user, int(row["provider_id"])) if row is not None else None
        if (
            row is None
            or not row["enabled"]
            or row["content"] is None
            or grant is None
            or grant["refreshing"]
        ):
            raise FeishuError("source_unavailable")
        return str(row["content"])

    def scan(self, limit: int = 20) -> None:
        for row in self.repo.due(int(self.clock()), min(max(limit, 0), 20)):
            try:
                self.sync(int(row["user_id"]), str(row["source_id"]))
            except FeishuError:
                continue
