"""Feishu / Lark web OAuth dashboard login adapter."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

import httpx
import jwt

from octop.infra.auth.sso.crypto import decrypt_secret
from octop.infra.auth.sso.public_base import oauth_callback_path
from octop.infra.db.repos.sso import SsoLoginStateRow, SsoProviderRow

if TYPE_CHECKING:
    from octop.infra.auth.sso.service import SsoService

_CN_MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")
_FEISHU_PHONE_SCOPE = "contact:user.phone:readonly"


def normalize_cn_mobile(raw: str | None) -> str:
    """Normalize a Feishu/ERP mobile string into a mainland China 11-digit number when applicable."""
    if not raw:
        return ""
    cleaned = re.sub(r"[\s\-()]+", "", str(raw).strip())
    if cleaned.startswith("+86"):
        cleaned = cleaned[3:]
    elif len(cleaned) == 13 and cleaned.startswith("861"):
        cleaned = cleaned[2:]
    digits = re.sub(r"\D+", "", cleaned)
    if _CN_MOBILE_RE.match(digits):
        return digits
    return digits


def mask_mobile(raw: str | None) -> str:
    """Return a masked display mobile (e.g. ``138****0000``), or empty when unavailable."""
    normalized = normalize_cn_mobile(raw)
    if len(normalized) >= 7:
        return f"{normalized[:3]}****{normalized[-4:]}"
    return ""

_FEISHU_ACCOUNTS = {
    "feishu": "https://accounts.feishu.cn",
    "lark": "https://accounts.larksuite.com",
}
_FEISHU_OPEN = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}


def feishu_region(row: SsoProviderRow) -> str:
    raw = row.extra.get("region")
    if raw == "lark":
        return "lark"
    return "feishu"


def _unwrap(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Feishu response is not an object")
    code = payload.get("code")
    if code not in (0, "0") and not (code is None and "access_token" in payload):
        err = payload.get("error_description") or payload.get("error") or payload.get("msg")
        detail = err if isinstance(err, str) and err else f"Feishu error {code}"
        raise ValueError(detail)
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    if data is None:
        # OAuth token responses may put access_token at the top level.
        return payload
    raise ValueError("Feishu response has no data object")


def _parse_response(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        response.raise_for_status()
        raise
    if response.is_error:
        _unwrap(payload)
        response.raise_for_status()
    return _unwrap(payload)


class FeishuAdapter:
    kind = "feishu"
    callback_path = oauth_callback_path()
    default_scopes = ""

    def __init__(self, service: SsoService) -> None:
        self._service = service

    def is_configured(self, row: SsoProviderRow) -> bool:
        return bool(row.client_id.strip() and row.client_secret_enc is not None)

    def authorize_url(
        self,
        *,
        row: SsoProviderRow,
        state: str,
        nonce: str,
        code_challenge: str,
        redirect_uri: str,
    ) -> str:
        del nonce  # Feishu authorize does not use OIDC nonce.
        region = feishu_region(row)
        params: dict[str, str] = {
            "client_id": row.client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        scope_tokens = [part for part in row.scopes.split() if part]
        if (
            bool(getattr(self._service._services.config.youli, "enabled", False))
            and _FEISHU_PHONE_SCOPE not in scope_tokens
        ):
            scope_tokens.append(_FEISHU_PHONE_SCOPE)
        scopes = " ".join(scope_tokens)
        if scopes:
            params["scope"] = scopes
        return f"{_FEISHU_ACCOUNTS[region]}/oauth/v3/authorize?{urlencode(params)}"

    def complete_login(
        self,
        code: str,
        *,
        row: SsoProviderRow,
        login_state: SsoLoginStateRow,
        redirect_uri: str,
    ) -> tuple[str, dict[str, Any]]:
        if row.client_secret_enc is None:
            raise ValueError("Feishu app secret is not configured")
        secret = decrypt_secret(self._service._services.secret_repo, row.client_secret_enc)
        open_base = _FEISHU_OPEN[feishu_region(row)]
        accounts_base = _FEISHU_ACCOUNTS[feishu_region(row)]
        with self._service._http_client() as client:
            token_response = client.post(
                f"{accounts_base}/oauth/v3/token",
                json={
                    "grant_type": "authorization_code",
                    "client_id": row.client_id,
                    "client_secret": secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": login_state.code_verifier,
                },
            )
            try:
                token_data = _parse_response(token_response)
            except (httpx.HTTPError, ValueError) as exc:
                raise ValueError(str(exc)) from exc
            access_token = token_data.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise jwt.InvalidTokenError("Feishu token response has no access_token")
            user_response = client.get(
                f"{open_base}/open-apis/authen/v1/user_info",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            try:
                identity = _parse_response(user_response)
            except (httpx.HTTPError, ValueError) as exc:
                raise ValueError(str(exc)) from exc
        subject = identity.get("union_id") or identity.get("open_id")
        if not isinstance(subject, str) or not subject:
            raise jwt.InvalidTokenError("Feishu user info has no union_id or open_id")
        tenant = identity.get("tenant_key")
        if not isinstance(tenant, str) or not tenant.strip():
            raise jwt.InvalidTokenError("Feishu user info has no tenant_key")
        scoped = "feishu-v1:" + json.dumps(
            [feishu_region(row), row.client_id, tenant, subject],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return scoped, self.to_claims(identity)

    def test_connection(self, row: SsoProviderRow) -> dict[str, bool | str]:
        if row.client_secret_enc is None:
            return {"ok": False, "detail": "Feishu app secret is not configured"}
        secret = decrypt_secret(self._service._services.secret_repo, row.client_secret_enc)
        open_base = _FEISHU_OPEN[feishu_region(row)]
        try:
            with self._service._http_client() as client:
                response = client.post(
                    f"{open_base}/open-apis/auth/v3/app_access_token/internal",
                    json={"app_id": row.client_id, "app_secret": secret},
                )
                response.raise_for_status()
                _unwrap(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "detail": str(exc)}
        return {"ok": True, "detail": "Feishu app credentials are valid"}

    @staticmethod
    def to_claims(identity: dict[str, Any]) -> dict[str, Any]:
        name = identity.get("name")
        email = identity.get("email") or identity.get("enterprise_email")
        mobile = identity.get("mobile")
        claims: dict[str, Any] = {}
        if isinstance(name, str) and name.strip():
            claims["name"] = name.strip()
        if isinstance(email, str) and email.strip():
            claims["email"] = email.strip()
        if isinstance(mobile, str) and mobile.strip():
            claims["mobile"] = mobile.strip()
        return claims
