"""HTTP client for the Youli (经贝 MCN) handoff endpoints."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

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
class YouliInnerLookupResult:
    status: Literal["ok", "ok_no_token", "not_found", "blocked"]
    reason: str = ""
    message: str = ""
    issued: YouliIssuedToken | None = None
    user_id: int = 0
    company_id: int = 0
    employee_id: int = 0
    username: str = ""
    nick_name: str = ""
    mobile: str = ""


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
            message = (
                (body.get("msg") or body.get("message") or body.get("error"))
                if isinstance(body, dict)
                else None
            )
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
            message = (
                (body.get("msg") or body.get("message") or body.get("error"))
                if isinstance(body, dict)
                else None
            )
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

    def _mdm_inner_headers(self) -> dict[str, str]:
        key = str(getattr(self._cfg, "inner_api_key", "") or "").strip()
        return {"X-Inner-Api-Key": key} if key else {}

    def _parse_inner_login_payload(
        self,
        body: Any,
        *,
        fallback_phone: str = "",
        default_terminal: int = 10,
    ) -> YouliInnerLookupResult:
        if not isinstance(body, dict) or body.get("code") != 200:
            msg = body.get("msg") if isinstance(body, dict) else None
            raise YouliClientError(str(msg or "youli MDM inner auth error"))
        data = body.get("data")
        if not isinstance(data, dict):
            raise YouliClientError("youli MDM inner auth returned invalid data")
        status = str(data.get("status") or "").strip()
        reason = str(data.get("reason") or "").strip()
        message = str(data.get("message") or "").strip()
        if status == "not_found":
            return YouliInnerLookupResult(
                status="not_found",
                reason=reason or "erp_not_found",
                message=message,
            )
        if status == "blocked":
            return YouliInnerLookupResult(
                status="blocked",
                reason=reason or "erp_disabled",
                message=message,
            )
        if status != "ok":
            raise YouliClientError(f"youli MDM inner auth returned unexpected status: {status!r}")
        token = data.get("token")
        user_id = int(data.get("userId") or 0)
        company_id = int(data.get("companyId") or 0)
        employee_id = int(data.get("employeeId") or 0)
        if not isinstance(token, str) or not token or not user_id or not company_id or not employee_id:
            raise YouliClientError("youli MDM inner auth returned incomplete credentials")
        username = str(data.get("username") or data.get("mobile") or fallback_phone or "")
        nick_name = str(data.get("nickName") or "")
        mobile = str(data.get("mobile") or fallback_phone or "")
        industry = int(data["industry"]) if data.get("industry") is not None else None
        terminal = int(data.get("terminal") or default_terminal)
        issued = YouliIssuedToken(
            token=token,
            username=username,
            user_id=user_id,
            company_id=company_id,
            employee_id=employee_id,
            industry=industry,
            terminal=terminal,
        )
        return YouliInnerLookupResult(
            status="ok",
            issued=issued,
            user_id=user_id,
            company_id=company_id,
            employee_id=employee_id,
            username=username,
            nick_name=nick_name,
            mobile=mobile,
        )

    def _mdm_request_nullable(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        from octop.infra.auth.youli.http_pool import perform_request

        url = f"{self._cfg.mdm_base_url.rstrip('/')}{path}"
        try:
            response = perform_request(
                httpx.Client,
                method,
                url,
                timeout=_TIMEOUT,
                verify=self._tls,
                params=params,
                target_label=f"mdm:{path}",
            )
        except httpx.HTTPError as exc:
            raise YouliClientUnavailable(f"youli MDM unreachable ({type(exc).__name__})") from exc
        if response.status_code >= 400:
            raise YouliClientError(f"youli MDM error {response.status_code} on {path}")
        raw_content = getattr(response, "content", None)
        if raw_content is not None and not raw_content:
            return None
        try:
            body = response.json()
        except ValueError as exc:
            raw_text = getattr(response, "text", None)
            if isinstance(raw_text, str) and not raw_text.strip():
                return None
            raise YouliClientUnavailable("youli MDM returned a non-JSON body") from exc
        if body is None:
            return None
        if not isinstance(body, dict):
            raise YouliClientUnavailable("youli MDM returned an unexpected body")
        if "code" in body and body.get("code") != 200:
            raise YouliClientError(
                f"youli MDM rejected {path}: {body.get('code')} {body.get('msg')}"
            )
        return body

    def _fallback_lookup_by_phone(self, phone: str) -> YouliInnerLookupResult:
        account = self._mdm_request_nullable(
            "POST", "/api/account/findByPhone", params={"phone": phone}
        )
        if not account or not account.get("id"):
            return YouliInnerLookupResult(
                status="not_found",
                reason="erp_not_found",
                message="未找到该手机号对应的 ERP 账号",
            )
        if int(account.get("status") or 0) != 1:
            return YouliInnerLookupResult(
                status="blocked",
                reason="erp_disabled",
                message="该 ERP 账号已停用",
            )
        user = self._mdm_request_nullable(
            "POST", "/api/user/findDefaultUserByPhone", params={"phone": phone}
        )
        if not user or not user.get("companyId") or not user.get("id"):
            return YouliInnerLookupResult(
                status="blocked",
                reason="erp_no_company",
                message="该 ERP 账号尚未绑定可用机构",
            )
        if user.get("status") is not None and int(user["status"]) != 1:
            return YouliInnerLookupResult(
                status="blocked",
                reason="erp_disabled",
                message="该 ERP 员工已离职或状态无效",
            )
        company_id = int(user["companyId"])
        employee_id = int(user["id"])
        login_user = self._mdm_request_nullable(
            "GET",
            "/api/user/getLoginUser",
            params={"companyId": company_id, "employeeId": employee_id},
        )
        if not login_user or int(login_user.get("deleted") or 0) != 0:
            return YouliInnerLookupResult(
                status="blocked",
                reason="erp_login_forbidden",
                message="当前 ERP 账号未开启登录许可，请联系管理员",
            )
        return YouliInnerLookupResult(
            status="ok_no_token",
            user_id=int(account["id"]),
            company_id=company_id,
            employee_id=employee_id,
            username=str(account.get("userName") or account.get("phone") or phone),
            nick_name=str(user.get("nickName") or ""),
            mobile=str(account.get("phone") or phone),
        )

    def _fallback_lookup_by_employee(
        self, *, company_id: int, employee_id: int
    ) -> YouliInnerLookupResult:
        login_user = self._mdm_request_nullable(
            "GET",
            "/api/user/getLoginUser",
            params={"companyId": company_id, "employeeId": employee_id},
        )
        if not login_user or int(login_user.get("deleted") or 0) != 0:
            return YouliInnerLookupResult(
                status="blocked",
                reason="erp_login_forbidden",
                message="当前 ERP 账号未开启登录许可，请联系管理员",
            )
        return YouliInnerLookupResult(
            status="ok_no_token",
            company_id=company_id,
            employee_id=employee_id,
        )

    def _post_inner_login(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        fallback_phone: str = "",
        default_terminal: int = 10,
    ) -> YouliInnerLookupResult | None:
        if not self._cfg.mdm_base_url:
            raise YouliClientError("youli MDM url is not configured")
        from octop.infra.auth.youli.http_pool import perform_request

        url = f"{self._cfg.mdm_base_url.rstrip('/')}{path}"
        try:
            response = perform_request(
                httpx.Client,
                "POST",
                url,
                timeout=_TIMEOUT,
                verify=self._tls,
                json=payload,
                headers=self._mdm_inner_headers() or None,
                target_label=f"mdm:{path}",
            )
        except httpx.HTTPError as exc:
            raise YouliClientUnavailable(f"youli MDM unreachable ({type(exc).__name__})") from exc
        if response.status_code == 404:
            logger.info("mdm %s returned 404; falling back to legacy lookup", path)
            return None
        try:
            body = response.json()
        except ValueError as exc:
            raise YouliClientUnavailable("youli MDM returned a non-JSON body") from exc
        if response.status_code >= 400:
            msg = body.get("msg") if isinstance(body, dict) else None
            raise YouliClientError(str(msg or f"youli MDM error {response.status_code}"))
        return self._parse_inner_login_payload(
            body, fallback_phone=fallback_phone, default_terminal=default_terminal
        )

    def inner_login_by_phone(
        self, *, phone: str, terminal: int = 10
    ) -> YouliInnerLookupResult:
        """Look up an ERP account by phone on MDM and issue a terminal-10 business token."""
        clean_phone = phone.strip()
        if not clean_phone:
            return YouliInnerLookupResult(status="not_found", reason="erp_not_found")
        res = self._post_inner_login(
            "/api/auth/inner/loginByPhone",
            {"phone": clean_phone, "terminal": terminal},
            fallback_phone=clean_phone,
            default_terminal=terminal,
        )
        if res is None:
            return self._fallback_lookup_by_phone(clean_phone)
        return res

    def inner_login_by_employee(
        self, *, company_id: int, employee_id: int, terminal: int = 10
    ) -> YouliInnerLookupResult:
        """Re-check an existing ERP employee on MDM and issue a fresh terminal-10 token."""
        res = self._post_inner_login(
            "/api/auth/inner/loginByEmployee",
            {"companyId": company_id, "employeeId": employee_id, "terminal": terminal},
            default_terminal=terminal,
        )
        if res is None:
            return self._fallback_lookup_by_employee(company_id=company_id, employee_id=employee_id)
        return res

    def _mdm_post_json(
        self,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        method: str = "POST",
    ) -> Any:
        if not self._cfg.mdm_base_url:
            raise YouliClientError("youli MDM url is not configured")
        from octop.infra.auth.youli.http_pool import perform_request

        url = f"{self._cfg.mdm_base_url.rstrip('/')}{path}"
        try:
            response = perform_request(
                httpx.Client,
                method,
                url,
                timeout=httpx.Timeout(20.0, connect=5.0),
                verify=self._tls,
                json=json_body,
                params=params,
                target_label=f"mdm:{path}",
            )
        except httpx.HTTPError as exc:
            raise YouliClientUnavailable(f"youli MDM unreachable ({type(exc).__name__})") from exc
        if response.status_code >= 400:
            raise YouliClientError(f"youli MDM error {response.status_code} on {path}")
        raw_content = getattr(response, "content", None)
        if raw_content is not None and not raw_content:
            return None
        try:
            body = response.json()
        except ValueError as exc:
            raw_text = getattr(response, "text", None)
            if isinstance(raw_text, str) and not raw_text.strip():
                return None
            raise YouliClientUnavailable("youli MDM returned a non-JSON body") from exc
        if isinstance(body, dict) and "code" in body and body.get("code") != 200:
            msg = body.get("msg") or body.get("message")
            raise YouliClientError(f"youli MDM rejected {path}: {body.get('code')} {msg}")
        return body

    def _fallback_org_directory_via_mdm(
        self,
        *,
        company_id: int,
        employee_id: int | None = None,
        include_departments: bool | None = None,
    ) -> YouliOrgDirectory:
        """Fetch departments & employees directly from MDM org endpoints when CRM org-directory is absent."""
        cid = int(company_id)
        depts: list[YouliOrgDept] = []
        visited_depts: set[int] = set()

        def _flatten_tree(nodes: Any) -> None:
            if not isinstance(nodes, list):
                return
            for node in nodes:
                if not isinstance(node, dict) or node.get("id") is None:
                    continue
                oid = int(node["id"])
                if oid not in visited_depts:
                    visited_depts.add(oid)
                    raw_parent = node.get("parentId")
                    raw_sort = node.get("sort")
                    depts.append(
                        YouliOrgDept(
                            org_id=oid,
                            parent_id=int(raw_parent) if raw_parent is not None else 1,
                            name=str(node.get("name") or oid),
                            org_code=str(node.get("orgCode") or ""),
                            sort=int(raw_sort) if raw_sort is not None else oid,
                            status=1,
                        )
                    )
                _flatten_tree(node.get("childOrg"))

        raw_tree = self._mdm_post_json(
            "/api/company/org/getByCompanyId",
            json_body={"companyId": cid, "parentId": 1},
        )
        tree_nodes = raw_tree.get("data") if isinstance(raw_tree, dict) else raw_tree
        _flatten_tree(tree_nodes)

        if not depts:
            raw_all = self._mdm_post_json(
                "/api/company/org/getAllDeparts",
                params={"companyId": cid},
                method="GET",
            )
            all_list = raw_all.get("data") if isinstance(raw_all, dict) else raw_all
            if isinstance(all_list, list):
                for org in all_list:
                    if not isinstance(org, dict) or org.get("id") is None:
                        continue
                    oid = int(org["id"])
                    if oid in visited_depts:
                        continue
                    visited_depts.add(oid)
                    raw_parent = org.get("parentId")
                    raw_sort = org.get("sort")
                    depts.append(
                        YouliOrgDept(
                            org_id=oid,
                            parent_id=int(raw_parent) if raw_parent is not None else 1,
                            name=str(org.get("name") or oid),
                            org_code=str(org.get("orgCode") or ""),
                            sort=int(raw_sort) if raw_sort is not None else oid,
                            status=1,
                        )
                    )

        root_org_id: int | None = None
        for d in depts:
            if d.parent_id == 1:
                root_org_id = d.org_id
                break
        if root_org_id is None and depts:
            root_org_id = depts[0].org_id

        emp_acc: dict[int, dict[str, Any]] = {}

        def _merge_emp_row(row: Any) -> None:
            if not isinstance(row, dict) or row.get("id") is None:
                return
            eid = int(row["id"])
            if employee_id is not None and eid != int(employee_id):
                return
            entry = emp_acc.get(eid)
            if entry is None:
                raw_acc = row.get("userId")
                raw_status = row.get("status")
                entry = {
                    "employee_id": eid,
                    "account_user_id": int(raw_acc) if raw_acc is not None else None,
                    "nick_name": str(row.get("nickName") or ""),
                    "phone": str(row.get("phone") or ""),
                    "employee_no": str(row.get("employeeNo") or ""),
                    "status": int(raw_status) if raw_status is not None else 1,
                    "is_admin": int(row.get("isAdmin") or 0),
                    "org_ids": [],
                    "admin_org_ids": [],
                    "_org_set": set(),
                    "_admin_org_set": set(),
                }
                emp_acc[eid] = entry
            if entry["account_user_id"] is None and row.get("userId") is not None:
                entry["account_user_id"] = int(row["userId"])
            if not entry["employee_no"] and row.get("employeeNo"):
                entry["employee_no"] = str(row["employeeNo"])
            if row.get("orgId") is not None:
                oid = int(row["orgId"])
                if oid not in entry["_org_set"]:
                    entry["_org_set"].add(oid)
                    entry["org_ids"].append(oid)
                if int(row.get("isCharger") or 0) == 1:
                    entry["_admin_org_set"].add(oid)
            for org in row.get("orgIds") or []:
                if isinstance(org, dict):
                    if org.get("id") is None:
                        continue
                    oid = int(org["id"])
                    if oid not in entry["_org_set"]:
                        entry["_org_set"].add(oid)
                        entry["org_ids"].append(oid)
                    if bool(org.get("charger")):
                        entry["_admin_org_set"].add(oid)
                elif org is not None:
                    oid = int(org)
                    if oid not in entry["_org_set"]:
                        entry["_org_set"].add(oid)
                        entry["org_ids"].append(oid)

        if root_org_id is not None:
            for page_num in range(1, 51):
                page_body = self._mdm_post_json(
                    "/api/company/org/listOrgUser",
                    json_body={
                        "companyId": cid,
                        "orgId": root_org_id,
                        "subOrgQuery": True,
                        "page": {"pageNum": page_num, "pageSize": 500},
                    },
                )
                rows = (
                    page_body.get("data")
                    if isinstance(page_body, dict)
                    else page_body
                )
                if not isinstance(rows, list) or not rows:
                    break
                for row in rows:
                    _merge_emp_row(row)
                if employee_id is not None and int(employee_id) in emp_acc:
                    break
                page_meta = page_body.get("page") if isinstance(page_body, dict) else None
                total_page = (
                    int(page_meta.get("totalPage") or 1)
                    if isinstance(page_meta, dict)
                    else 1
                )
                if page_num >= total_page:
                    break

        if not emp_acc or (employee_id is not None and int(employee_id) not in emp_acc):
            raw_all_users = self._mdm_post_json(
                "/api/company/org/getAllUser",
                json_body=cid,
            )
            all_rows = (
                raw_all_users.get("data")
                if isinstance(raw_all_users, dict)
                else raw_all_users
            )
            if isinstance(all_rows, list):
                for row in all_rows:
                    _merge_emp_row(row)

        emps = tuple(
            YouliOrgEmployee(
                employee_id=e["employee_id"],
                account_user_id=e["account_user_id"],
                nick_name=e["nick_name"],
                phone=e["phone"],
                employee_no=e["employee_no"],
                status=e["status"],
                is_admin=e["is_admin"],
                org_ids=tuple(e["org_ids"]),
                admin_org_ids=tuple(sorted(e["_admin_org_set"])),
            )
            for e in emp_acc.values()
        )
        need_depts = (
            bool(include_departments)
            if include_departments is not None
            else (employee_id is None)
        )
        return YouliOrgDirectory(
            company_id=cid,
            synced_at_epoch=int(time.time()),
            departments=tuple(depts) if need_depts else (),
            employees=emps,
        )

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
        try:
            data = self._post("org-directory", payload)
        except YouliClientUnavailable:
            raise
        except YouliClientError as exc:
            if not self._cfg.mdm_base_url:
                raise
            logger.info(
                "crm org-directory failed (%s); falling back to MDM org endpoints",
                exc,
            )
            return self._fallback_org_directory_via_mdm(
                company_id=company_id,
                employee_id=employee_id,
                include_departments=include_departments,
            )

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
