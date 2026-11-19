"""HTTP client for the Youli (经贝 MCN) handoff endpoints."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from octop.config import YouliConfig
from octop.infra.auth.youli.tls import build_verify

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_DELEGATION_TERMINAL_BASE = 1_000_000_000

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


@dataclass(frozen=True)
class YouliOrgDept:
    org_id: int
    parent_id: int
    name: str
    org_code: str = ""
    sort: int = 0
    status: int = 1


@dataclass(frozen=True)
class YouliOrgEmployee:
    employee_id: int
    account_user_id: int | None
    nick_name: str
    phone: str = ""
    employee_no: str = ""
    status: int = 1
    is_admin: int = 0
    org_ids: tuple[int, ...] = ()
    admin_org_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class YouliOrgDirectory:
    company_id: int
    synced_at_epoch: int
    departments: tuple[YouliOrgDept, ...]
    employees: tuple[YouliOrgEmployee, ...]


class YouliClient:
    def __init__(self, cfg: YouliConfig) -> None:
        self._cfg = cfg
        self._tls = build_verify(cfg.ca_file)

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
        from octop.infra.auth.youli.http_pool import perform_request

        try:
            response = perform_request(
                httpx.Client,
                "POST",
                self._url(suffix),
                timeout=_TIMEOUT,
                verify=self._tls,
                json=payload,
                target_label=f"youli:{suffix}",
            )
        except httpx.HTTPError as exc:
            raise YouliClientUnavailable(
                f"youli upstream unreachable ({type(exc).__name__})"
            ) from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise YouliClientUnavailable("youli upstream returned a non-JSON body") from exc
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
        return self._issued_token(data)

    def issue_delegation(self, *, mobile: str, password: str) -> YouliIssuedToken:
        data = self._post(
            "delegation",
            {
                "clientId": self._cfg.client_id,
                "clientSecret": self._cfg.client_secret,
                "audience": self._cfg.audience,
                "mobile": mobile,
                "password": password,
            },
        )
        issued = self._issued_token(data)
        if issued.terminal <= _DELEGATION_TERMINAL_BASE:
            raise YouliClientError("ERP returned an invalid delegation terminal")
        return issued

    @staticmethod
    def _issued_token(data: dict[str, Any]) -> YouliIssuedToken:
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise YouliClientError("ERP returned no token")
        return YouliIssuedToken(
            token=token,
            username=str(data.get("username") or ""),
            user_id=int(data.get("userId") or 0),
            company_id=int(data.get("companyId") or 0),
            employee_id=int(data.get("employeeId") or 0),
            industry=(int(data["industry"]) if data.get("industry") is not None else None),
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
        from octop.infra.auth.youli.http_pool import perform_request

        try:
            response = perform_request(
                httpx.Client,
                "POST",
                url,
                timeout=_TIMEOUT,
                verify=self._tls,
                json=payload,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise YouliClientUnavailable(
                f"youli upstream unreachable ({type(exc).__name__})"
            ) from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise YouliClientUnavailable("youli upstream returned a non-JSON body") from exc
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
        from octop.infra.auth.youli.http_pool import perform_request

        try:
            response = perform_request(
                httpx.Client,
                "POST",
                f"{self._cfg.mdm_base_url.rstrip('/')}/api/auth/verify",
                timeout=_TIMEOUT,
                verify=self._tls,
                content=token.encode("utf-8"),
                headers={"Content-Type": "application/json"},
                target_label="mdm:/api/auth/verify",
            )
        except httpx.HTTPError as exc:
            raise YouliClientUnavailable(f"youli MDM unreachable ({type(exc).__name__})") from exc
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

    def fetch_org_directory(
        self,
        *,
        company_id: int,
        employee_id: int | None = None,
        include_departments: bool | None = None,
    ) -> YouliOrgDirectory:
        """Fetch Huihuo ERP department tree and employee directory (read-only)."""
        payload: dict[str, Any] = {
            "clientId": self._cfg.client_id,
            "clientSecret": self._cfg.client_secret,
            "audience": self._cfg.audience,
            "companyId": int(company_id),
        }
        if employee_id is not None:
            payload["employeeId"] = int(employee_id)
        if include_departments is not None:
            payload["includeDepartments"] = bool(include_departments)
        data = self._post("org-directory", payload)

        depts: list[YouliOrgDept] = []
        for raw in data.get("departments") or []:
            if not isinstance(raw, dict) or raw.get("orgId") is None:
                continue
            org_id = int(raw["orgId"])
            depts.append(
                YouliOrgDept(
                    org_id=org_id,
                    parent_id=int(raw.get("parentId") if raw.get("parentId") is not None else 1),
                    name=str(raw.get("name") or org_id),
                    org_code=str(raw.get("orgCode") or ""),
                    sort=int(raw.get("sort") if raw.get("sort") is not None else org_id),
                    status=int(raw.get("status") if raw.get("status") is not None else 1),
                )
            )

        emps: list[YouliOrgEmployee] = []
        for raw in data.get("employees") or []:
            if not isinstance(raw, dict) or raw.get("employeeId") is None:
                continue
            raw_acc = raw.get("accountUserId")
            emps.append(
                YouliOrgEmployee(
                    employee_id=int(raw["employeeId"]),
                    account_user_id=int(raw_acc) if raw_acc is not None else None,
                    nick_name=str(raw.get("nickName") or ""),
                    phone=str(raw.get("phone") or ""),
                    employee_no=str(raw.get("employeeNo") or ""),
                    status=int(raw.get("status") if raw.get("status") is not None else 1),
                    is_admin=int(raw.get("isAdmin") or 0),
                    org_ids=tuple(int(x) for x in (raw.get("orgIds") or []) if x is not None),
                    admin_org_ids=tuple(
                        int(x) for x in (raw.get("adminOrgIds") or []) if x is not None
                    ),
                )
            )

        return YouliOrgDirectory(
            company_id=int(data.get("companyId") or company_id),
            synced_at_epoch=int(data.get("syncedAtEpoch") or 0),
            departments=tuple(depts),
            employees=tuple(emps),
        )
