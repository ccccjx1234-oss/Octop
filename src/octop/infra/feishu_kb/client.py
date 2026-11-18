"""Feishu document-grant protocol; persistence and access policy live elsewhere.

Calls are synchronous and must be run off the event loop by async callers.
This module intentionally does not publish documents or store credentials.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

_ACCOUNTS = "https://accounts.feishu.cn"
_OPEN = "https://open.feishu.cn"
_DOCUMENT_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class FeishuError(Exception):
    """Sanitized upstream failure, safe to record without response contents."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Grant:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_in: int


def authorize_url(*, client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "docx:document:readonly offline_access",
        }
    )
    return f"{_ACCOUNTS}/oauth/v3/authorize?{query}"


def _data(response: httpx.Response) -> dict[str, object]:
    try:
        body = response.json()
    except ValueError as exc:
        raise FeishuError("invalid_response") from exc
    if not isinstance(body, dict):
        raise FeishuError("invalid_response")
    code = body.get("code", 0)
    if code not in (0, "0"):
        raise FeishuError("upstream_error")
    result = body.get("data", body)
    if not isinstance(result, dict):
        raise FeishuError("invalid_response")
    return result


class FeishuClient:
    def __init__(
        self,
        http: httpx.Client,
        *,
        client_id: str,
        client_secret: str,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._sleep = sleep

    def close(self) -> None:
        self._http.close()

    def exchange(self, *, code: str, verifier: str, redirect_uri: str) -> Grant:
        return self._token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            }
        )

    def refresh(self, refresh_token: str) -> Grant:
        # Never retry: a lost response may already have consumed the old token.
        return self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def _token(self, fields: dict[str, str]) -> Grant:
        try:
            response = self._http.post(
                f"{_ACCOUNTS}/oauth/v3/token",
                json={**fields, "client_id": self._client_id, "client_secret": self._client_secret},
            )
        except httpx.RequestError:
            raise FeishuError("token_unavailable") from None
        if not response.is_success:
            raise FeishuError("token_unavailable")
        data = _data(response)
        access = data.get("access_token")
        refresh = data.get("refresh_token")
        expires = data.get("expires_in")
        if (
            not isinstance(access, str)
            or not access
            or not isinstance(refresh, str)
            or not refresh
            or not isinstance(expires, int)
            or isinstance(expires, bool)
            or expires <= 0
        ):
            raise FeishuError("invalid_token_response")
        return Grant(access, refresh, expires)

    def subject(self, access_token: str) -> str:
        try:
            response = self._http.get(
                f"{_OPEN}/open-apis/authen/v1/user_info",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.RequestError:
            raise FeishuError("identity_unavailable") from None
        if not response.is_success:
            raise FeishuError("identity_unavailable")
        data = _data(response)
        tenant = data.get("tenant_key")
        subject = data.get("union_id") or data.get("open_id")
        if not isinstance(tenant, str) or not tenant.strip():
            raise FeishuError("invalid_identity")
        if not isinstance(subject, str) or not subject:
            raise FeishuError("invalid_identity")
        return "feishu-v1:" + json.dumps(
            ["feishu", self._client_id, tenant, subject], ensure_ascii=True, separators=(",", ":")
        )

    def raw_content(self, document_id: str, access_token: str) -> str:
        if not _DOCUMENT_ID.fullmatch(document_id):
            raise FeishuError("invalid_document_id")
        for attempt in range(3):
            try:
                response = self._http.get(
                    f"{_OPEN}/open-apis/docx/v1/documents/{document_id}/raw_content",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            except httpx.RequestError:
                response = None
            if response is not None:
                try:
                    body = response.json()
                except ValueError:
                    body = None
                code = body.get("code") if isinstance(body, dict) else None
                if response.status_code == 404 or code in (1770002, 1770003):
                    raise FeishuError("missing")
                if response.status_code in (401, 403) or code == 1770032:
                    raise FeishuError("forbidden")
                transient = response.status_code == 429 or response.status_code >= 500
                transient = transient or code == 99991400
                if not transient:
                    if not response.is_success:
                        raise FeishuError("upstream_error")
                    content = _data(response).get("content")
                    if not isinstance(content, str):
                        raise FeishuError("invalid_response")
                    return content
            if attempt < 2:
                self._sleep(0.5 * (2**attempt))
        raise FeishuError("unavailable")
