"""HTTP client for the Youli (经贝 MCN) handoff endpoints."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from octop.config import YouliConfig

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

logger = logging.getLogger(__name__)


class YouliClientError(Exception):
    """Upstream rejected the call or is unreachable."""


class YouliClientUnavailable(YouliClientError):
    """The upstream could not be reached or answered with a broken body."""


@dataclass(frozen=True)
class YouliIssuedToken:
    token: str
    username: str
    user_id: int
    company_id: int
    employee_id: int
    industry: int | None
    terminal: int


class YouliClient:
    def __init__(self, cfg: YouliConfig) -> None:
        self._cfg = cfg

    @property
    def enabled(self) -> bool:
        return bool(
            self._cfg.enabled
            and self._cfg.base_url
            and self._cfg.client_id
            and self._cfg.client_secret
        )

    def _url(self, suffix: str) -> str:
        return f"{self._cfg.base_url.rstrip('/')}/api/crm/sso/youli/{suffix}"

    def _post(self, suffix: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise YouliClientError("youli integration is not configured")
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                response = client.post(self._url(suffix), json=payload)
        except httpx.HTTPError as exc:
            raise YouliClientUnavailable("youli upstream unreachable") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise YouliClientUnavailable(
                "youli upstream returned a non-JSON body"
            ) from exc
        code = body.get("code") if isinstance(body, dict) else None
        if response.status_code >= 400 or code != 200:
            message = body.get("msg") if isinstance(body, dict) else None
            logger.warning(
                "youli %s rejected: http=%s code=%s msg=%s",
                suffix,
                response.status_code,
                code,
                message,
            )
            raise YouliClientError(str(message or "youli upstream error"))
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def register(
        self,
        *,
        request_id: str,
        audience: str,
        callback_url: str,
        code_challenge: str,
        expires_at_epoch: int,
    ) -> None:
        self._post(
            "register",
            {
                "clientId": self._cfg.client_id,
                "clientSecret": self._cfg.client_secret,
                "requestId": request_id,
                "audience": audience,
                "callbackUri": callback_url,
                "codeChallenge": code_challenge,
                "expiresAtEpoch": expires_at_epoch,
            },
        )

    def redeem(
        self,
        *,
        code: str,
        request_id: str,
        audience: str,
        code_verifier: str,
    ) -> YouliIssuedToken:
        data = self._post(
            "redeem",
            {
                "clientId": self._cfg.client_id,
                "clientSecret": self._cfg.client_secret,
                "code": code,
                "requestId": request_id,
                "audience": audience,
                "codeVerifier": code_verifier,
            },
        )
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise YouliClientError("youli redeem returned no token")
        return YouliIssuedToken(
            token=token,
            username=str(data.get("username") or ""),
            user_id=int(data.get("userId") or 0),
            company_id=int(data.get("companyId") or 0),
            employee_id=int(data.get("employeeId") or 0),
            industry=(
                int(data["industry"]) if data.get("industry") is not None else None
            ),
            terminal=int(data.get("terminal") or 0),
        )

    def revoke(
        self,
        *,
        username: str,
        company_id: int,
        industry: int | None,
    ) -> None:
        self._post(
            "revoke",
            {
                "clientId": self._cfg.client_id,
                "clientSecret": self._cfg.client_secret,
                "username": username,
                "companyId": company_id,
                "industry": industry,
            },
        )

    def password_login(self, *, mobile: str, password: str) -> str:
        """Sign in with the employee account and return the MCN business token."""
        # Terminal 10 is the Octop slot, so this login does not replace the PC session.
        data = self._post_absolute(
            f"{self._cfg.base_url.rstrip('/')}/api/crm/user/passwordLogin",
            {"mobile": mobile, "password": password, "terminal": 10},
        )
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise YouliClientError("youli login returned no token")
        return token

    def issue_code(self, *, request_id: str, audience: str, token: str) -> str:
        """Ask MCN to sign one handoff code for an already signed-in employee."""
        data = self._post_absolute(
            f"{self._cfg.base_url.rstrip('/')}/api/crm/sso/youli/issue",
            {"requestId": request_id, "audience": audience},
            headers={"Auth-Token": token},
        )
        code = data.get("code")
        if not isinstance(code, str) or not code:
            raise YouliClientError("youli issue returned no code")
        return code

    def _post_absolute(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise YouliClientError("youli integration is not configured")
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                response = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise YouliClientUnavailable("youli upstream unreachable") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise YouliClientUnavailable(
                "youli upstream returned a non-JSON body"
            ) from exc
        code = body.get("code") if isinstance(body, dict) else None
        if response.status_code >= 400 or code != 200:
            message = body.get("msg") if isinstance(body, dict) else None
            logger.warning(
                "youli %s rejected: http=%s code=%s msg=%s",
                url,
                response.status_code,
                code,
                message,
            )
            raise YouliClientError(str(message or "youli upstream error"))
        data = body.get("data")
        return data if isinstance(data, dict) else {}

    def verify(self, token: str) -> dict[str, Any]:
        """Verify the business token against MDM; fails closed on any ambiguity."""
        if not self._cfg.mdm_base_url:
            raise YouliClientError("youli MDM url is not configured")
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                response = client.post(
                    f"{self._cfg.mdm_base_url.rstrip('/')}/api/auth/verify",
                    content=token.encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise YouliClientUnavailable("youli MDM unreachable") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise YouliClientUnavailable("youli MDM returned a non-JSON body") from exc
        if not isinstance(body, dict):
            raise YouliClientUnavailable("youli MDM returned an unexpected body")
        if response.status_code >= 400 or ("code" in body and body.get("code") != 200):
            raise YouliClientError(
                f"youli MDM rejected the token: {body.get('code')} {body.get('msg')}"
            )
        if not body.get("userId") or not body.get("companyId"):
            raise YouliClientError("youli MDM returned an incomplete identity")
        return body

