"""Fresh Feishu proof for one ERP binding attempt."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any

from octop.infra.auth.sso.crypto import decrypt_secret, encrypt_secret
from octop.infra.auth.sso.pkce import new_pkce_pair
from octop.infra.auth.sso.providers.feishu import FeishuAdapter, feishu_region
from octop.infra.db.repos.feishu_erp_reauth import FeishuErpReauthRepo, config_fingerprint
from octop.infra.db.repos.sso import SsoLoginStateRow
from octop.infra.errors import ErrorCode, OctopError


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def denied() -> OctopError:
    return OctopError(ErrorCode.AUTH_FAILED, "Feishu reauthentication failed")


class FeishuErpReauth:
    def __init__(self, services: Any, sso: Any) -> None:
        self.services = services
        self.repo = FeishuErpReauthRepo(services.db)
        self.adapter = FeishuAdapter(sso)

    def start(self, user: int, session: str, provider_id: int) -> str:
        provider = self.services.sso_repo.get_by_id(provider_id)
        if (
            not provider
            or provider.kind != "feishu"
            or not provider.enabled
            or not self.adapter.is_configured(provider)
            or not provider.dashboard_origin
        ):
            raise denied()
        identities = self.services.user_repo.list_sso_identities(user)
        subject = next((i.subject for i in identities if i.provider_id == provider_id), "")
        try:
            parts = json.loads(subject.removeprefix("feishu-v1:"))
        except ValueError:
            raise denied() from None
        if (
            not subject.startswith("feishu-v1:")
            or not isinstance(parts, list)
            or len(parts) != 4
            or parts[:2] != [feishu_region(provider), provider.client_id]
            or not all(isinstance(v, str) and v for v in parts[2:])
        ):
            raise denied()
        state = secrets.token_urlsafe(32)
        verifier, challenge = new_pkce_pair()
        redirect = provider.dashboard_origin.rstrip("/") + "/youli/reauth/callback"
        now = int(time.time())
        row = {
            "state_hash": digest(state),
            "user_id": user,
            "provider_id": provider_id,
            "session_hash": digest(session),
            "subject": subject,
            "client_id": provider.client_id,
            "region": feishu_region(provider),
            "config_fingerprint": config_fingerprint(vars(provider)),
            "verifier_blob": encrypt_secret(self.services.secret_repo, verifier),
            "redirect_uri": redirect,
            "expires_at": now + 300,
        }
        if not self.repo.create(row, now):
            raise denied()
        return (
            self.adapter.authorize_url(
                row=provider, state=state, nonce="", code_challenge=challenge, redirect_uri=redirect
            )
            + "&prompt=consent"
        )

    def callback(self, user: int, session: str, state: str, code: str) -> str:
        now = int(time.time())
        key = digest(state)
        row = self.repo.consume_state(key, user, digest(session), now)
        if row is None:
            raise denied()
        provider = self.services.sso_repo.get_by_id(int(row["provider_id"]))
        if (
            not provider
            or not provider.enabled
            or provider.kind != "feishu"
            or provider.client_id != row["client_id"]
            or feishu_region(provider) != row["region"]
            or not self.adapter.is_configured(provider)
        ):
            raise denied()
        login_state = SsoLoginStateRow(
            state="",
            provider_id=provider.id,
            nonce="",
            code_verifier=decrypt_secret(self.services.secret_repo, bytes(row["verifier_blob"])),
            redirect_after="",
            login_code=None,
            user_id=user,
            expires_at=int(row["expires_at"]),
            consumed_at=now,
            created_at=now,
        )
        try:
            subject, _ = self.adapter.complete_login(
                code, row=provider, login_state=login_state, redirect_uri=row["redirect_uri"]
            )
        except Exception as exc:
            raise denied() from exc
        if subject != row["subject"]:
            raise denied()
        proof = secrets.token_urlsafe(32)
        if not self.repo.finish(key, user, provider.id, digest(proof), int(time.time())):
            raise denied()
        return proof

    def claim(self, user: int, session: str, proof: str) -> Any:
        row = self.repo.claim(digest(proof), user, digest(session), int(time.time()))
        if row is None:
            raise denied()
        return row
