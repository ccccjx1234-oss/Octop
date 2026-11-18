"""Youli handoff orchestration: pending request, code exchange, sid sessions."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urlparse

from octop.config import YouliConfig
from octop.infra.auth.sso.crypto import decrypt_secret, encrypt_secret
from octop.infra.auth.sso.pkce import new_pkce_pair
from octop.infra.auth.tokens import PURPOSE_ACCESS, sign_token
from octop.infra.auth.youli.client import (
    YouliClient,
    YouliClientError,
    YouliClientUnavailable,
    YouliIssuedToken,
)
from octop.infra.db.repos.youli import YouliSessionRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.identity import User
from octop.infra.users.manager import UserManager

logger = logging.getLogger(__name__)

YOULI_PROVIDER_KIND = "youli"
YOULI_ROLE_ASSIGNMENT: tuple[str, str | None, list[str], list[tuple[str, str]]] = (
    "youli_employee",
    "会火员工",
    ["connector_enabled", "connector_builtin"],
    [],
)

REASON_SESSION_REPLACED = "session_replaced"
REASON_UPSTREAM_EXPIRED = "upstream_expired"
_SESSION_REPLACED_MESSAGE = "当前账号已在其他设备或窗口登录，当前会话已下线，请重新登录。"

_REVOKED_REASONS_LOCK = threading.Lock()
_REVOKED_SID_REASONS: dict[str, str] = {}


def record_sid_revocation_reason(sid: str, reason: str) -> None:
    """Remember why a Youli sid was revoked (bounded in-memory cache)."""
    clean_sid = sid.strip()
    if not clean_sid or not reason:
        return
    with _REVOKED_REASONS_LOCK:
        if len(_REVOKED_SID_REASONS) >= 4096:
            _REVOKED_SID_REASONS.clear()
        _REVOKED_SID_REASONS[clean_sid] = reason


def get_sid_revocation_reason(sid: str) -> str | None:
    clean_sid = sid.strip()
    if not clean_sid:
        return None
    with _REVOKED_REASONS_LOCK:
        return _REVOKED_SID_REASONS.get(clean_sid)


def resolve_youli_revocation_reason(
    repo: Any,
    *,
    sid: str,
    user_id: int,
) -> str | None:
    """Infer whether *sid* was replaced by a newer login or otherwise revoked."""
    recorded = get_sid_revocation_reason(sid)
    if recorded is not None:
        return recorded
    row = repo.get(sid)
    if row is not None and not row.active:
        newer = [
            s
            for s in repo.list_active_for_user(user_id)
            if s.sid != sid and s.created_at >= row.created_at
        ]
        if newer:
            return REASON_SESSION_REPLACED
        return REASON_UPSTREAM_EXPIRED
    return None


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cookie_name(request_id: str) -> str:
    return f"youli_sso_{request_id}"


@dataclass(frozen=True)
class YouliStartResult:
    request_id: str
    state: str
    entry_url: str
    callback_url: str
    browser_token: str
    cookie_name: str
    cookie_path: str
    max_age: int


@dataclass(frozen=True)
class YouliExchangeResult:
    user: User
    access_token: str
    expires_in: int
    session: YouliSessionRow


class YouliAuthService:
    def __init__(
        self,
        *,
        services: SharedServices,
        users: UserManager,
        cfg: YouliConfig,
        ws_hub: Any = None,
    ) -> None:
        self._services = services
        self._users = users
        self._cfg = cfg
        self._ws_hub = ws_hub
        self._client = YouliClient(cfg)

    @property
    def enabled(self) -> bool:
        return self._client.enabled and bool(self._cfg.callback_url)

    def ensure_provider(self) -> int:
        row = self._services.sso_repo.upsert_by_kind(
            YOULI_PROVIDER_KIND,
            enabled=True,
            display_name="经贝 MCN",
            issuer=self._cfg.base_url or "youli",
            client_id=self._cfg.client_id,
            client_secret_enc=None,
            scopes="",
            dashboard_origin=None,
            extra={"audience": self._cfg.audience},
        )
        return row.id

    async def start(self, *, public_base: str) -> YouliStartResult:
        if not self.enabled:
            raise OctopError(ErrorCode.INTERNAL_ERROR, "youli integration is not configured")
        if not self._cfg.business_entry_url:
            raise OctopError(ErrorCode.INTERNAL_ERROR, "youli business entry is not configured")
        request_id = uuid.uuid4().hex
        state = secrets.token_urlsafe(24)
        verifier, challenge = new_pkce_pair()
        browser_token = secrets.token_urlsafe(32)
        now = int(time.time())
        expires_at = now + int(self._cfg.request_ttl_seconds)
        try:
            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._client.register(
                    request_id=request_id,
                    audience=self._cfg.audience,
                    callback_url=self._cfg.callback_url,
                    code_challenge=challenge,
                    expires_at_epoch=expires_at,
                ),
            )
        except YouliClientUnavailable as exc:
            logger.warning("youli start unavailable: %s", exc)
            raise OctopError(ErrorCode.NETWORK_ERROR, str(exc), status=503) from exc
        except YouliClientError as exc:
            logger.warning("youli start rejected: %s", exc)
            raise OctopError(ErrorCode.AUTH_FAILED, str(exc)) from exc
        self._services.youli_login_request_repo.create(
            request_id=request_id,
            state_hash=_sha256(state),
            client_id=self._cfg.client_id,
            audience=self._cfg.audience,
            callback_url=self._cfg.callback_url,
            verifier_hash=_sha256(verifier),
            verifier_blob=encrypt_secret(self._services.secret_repo, verifier),
            browser_hash=_sha256(browser_token),
            expires_at=expires_at,
        )
        query = urlencode(
            {
                "request_id": request_id,
                "state": state,
                "audience": self._cfg.audience,
                "callback": self._cfg.callback_url,
            }
        )
        separator = "&" if "?" in self._cfg.business_entry_url else "?"
        entry_url = f"{self._cfg.business_entry_url}{separator}{query}"
        return YouliStartResult(
            request_id=request_id,
            state=state,
            entry_url=entry_url,
            callback_url=self._cfg.callback_url,
            browser_token=browser_token,
            cookie_name=_cookie_name(request_id),
            cookie_path=self._cookie_path(),
            max_age=int(self._cfg.request_ttl_seconds),
        )

    async def staff_login(
        self,
        *,
        mobile: str,
        password: str,
        public_base: str,
    ) -> YouliExchangeResult:
        """Sign in on the HuiHuo page, then finish the MCN handoff server side."""
        started = await self.start(public_base=public_base)
        loop = asyncio.get_running_loop()
        try:
            token = await loop.run_in_executor(
                None,
                lambda: self._client.password_login(mobile=mobile, password=password),
            )
            code = await loop.run_in_executor(
                None,
                lambda: self._client.issue_code(
                    request_id=started.request_id,
                    audience=self._cfg.audience,
                    token=token,
                ),
            )
        except YouliClientUnavailable as exc:
            self._services.youli_login_request_repo.consume(started.request_id)
            logger.warning("youli staff login unavailable: %s", exc)
            raise OctopError(ErrorCode.NETWORK_ERROR, str(exc), status=503) from exc
        except YouliClientError as exc:
            self._services.youli_login_request_repo.consume(started.request_id)
            logger.warning("youli staff login rejected: %s", exc)
            raise OctopError(ErrorCode.AUTH_FAILED, str(exc)) from exc
        return await self.exchange(
            code=code,
            state=started.state,
            request_id=started.request_id,
            browser_token=started.browser_token,
        )

    async def exchange(
        self,
        *,
        code: str,
        state: str,
        request_id: str,
        browser_token: str,
    ) -> YouliExchangeResult:
        issued = await self._redeem_verified(
            code=code,
            state=state,
            request_id=request_id,
            browser_token=browser_token,
            compensate=True,
        )
        now = int(time.time())
        sid = uuid.uuid4().hex
        ttl = int(self._cfg.token_ttl_seconds)
        abs_expires = now + min(int(self._services.config.access_token_ttl_seconds), ttl)
        try:
            user_row, session, replaced_sids = self._services.youli_session_repo.login(
                provider_id=self.ensure_provider(),
                subject=f"youli:{issued.company_id}:{issued.employee_id}",
                role=YOULI_ROLE_ASSIGNMENT[0],
                role_name=YOULI_ROLE_ASSIGNMENT[1] or "",
                permissions=YOULI_ROLE_ASSIGNMENT[2],
                sid=sid,
                account_user_id=issued.user_id,
                account_username=issued.username,
                company_id=issued.company_id,
                employee_id=issued.employee_id,
                industry=issued.industry,
                token_blob=encrypt_secret(self._services.secret_repo, issued.token),
                token_expires_at=now + ttl,
                abs_expires_at=abs_expires,
            )
        except ValueError as exc:
            raise OctopError(
                ErrorCode.AUTH_FAILED, "employee identity reserved for delegation"
            ) from exc
        user = User(
            id=user_row.id, username=user_row.username, role=str(user_row.role),
            display_name=user_row.display_name, locale=user_row.locale,
            permissions=list(user_row.permissions),
        )
        self._users.register_cached_user(user)
        for old_sid in replaced_sids:
            record_sid_revocation_reason(old_sid, REASON_SESSION_REPLACED)
            if self._ws_hub is not None:
                try:
                    await self._ws_hub.push_to_sid(
                        old_sid,
                        {
                            "type": "session_revoked",
                            "reason": REASON_SESSION_REPLACED,
                            "message": _SESSION_REPLACED_MESSAGE,
                        },
                    )
                except Exception:  # noqa: BLE001 - best-effort notification
                    logger.debug("youli session_revoked push failed for sid %s", old_sid)
        secret = self._services.secret_repo.get("jwt")
        if secret is None:
            raise OctopError(ErrorCode.INTERNAL_ERROR, "jwt secret missing")
        user_row = self._services.user_repo.get(user.id)
        access_token = sign_token(
            secret,
            sub=user.id,
            uname=user.username,
            role=user.role,
            purpose=PURPOSE_ACCESS,
            mfa_version=int(user_row.mfa_version) if user_row is not None else 0,
            password_version=int(user_row.password_version) if user_row is not None else 0,
            ttl_seconds=abs_expires - now,
            sid=sid,
        )
        return YouliExchangeResult(
            user=user,
            access_token=access_token,
            expires_in=max(1, abs_expires - now),
            session=session,
        )

    async def _redeem_verified(
        self,
        *,
        code: str,
        state: str,
        request_id: str,
        browser_token: str,
        compensate: bool = False,
    ) -> YouliIssuedToken:
        request = self._services.youli_login_request_repo.get(request_id)
        if request is None:
            raise OctopError(ErrorCode.AUTH_FAILED, "unknown login request")
        now = int(time.time())
        if request.consumed_at is not None or request.expires_at <= now:
            raise OctopError(ErrorCode.AUTH_FAILED, "login request expired")
        if not secrets.compare_digest(request.state_hash, _sha256(state or "")):
            raise OctopError(ErrorCode.AUTH_FAILED, "login request state mismatch")
        if not secrets.compare_digest(request.browser_hash, _sha256(browser_token or "")):
            raise OctopError(ErrorCode.AUTH_FAILED, "login request is not bound to this browser")
        if request.verifier_blob is None:
            raise OctopError(ErrorCode.AUTH_FAILED, "login verifier missing")
        verifier = decrypt_secret(self._services.secret_repo, request.verifier_blob)
        if not self._services.youli_login_request_repo.consume(request_id):
            raise OctopError(ErrorCode.AUTH_FAILED, "login request already used")
        try:
            issued = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._client.redeem(
                    code=code,
                    request_id=request_id,
                    audience=request.audience,
                    code_verifier=verifier,
                ),
            )
        except YouliClientError as exc:
            raise OctopError(ErrorCode.AUTH_FAILED, "handoff exchange failed") from exc
        try:
            await self._verify_identity(issued)
        except Exception:
            if compensate:
                await self._revoke_upstream(issued)
            raise
        return issued

    async def bind_employee(
        self, *, user_id: int, mobile: str, password: str, public_base: str, feishu_claim: Any
    ) -> None:
        """Prove the employee account without changing the current Octop login."""
        if not self._cfg.mdm_base_url:
            raise OctopError(
                ErrorCode.NETWORK_ERROR, "business verification unavailable", status=503
            )
        started = await self.start(public_base=public_base)
        loop = asyncio.get_running_loop()
        try:
            token = await loop.run_in_executor(
                None, lambda: self._client.password_login(mobile=mobile, password=password)
            )
            code = await loop.run_in_executor(
                None,
                lambda: self._client.issue_code(
                    request_id=started.request_id, audience=self._cfg.audience, token=token
                ),
            )
            issued = await self._redeem_verified(
                code=code,
                state=started.state,
                request_id=started.request_id,
                browser_token=started.browser_token,
            )
        except YouliClientUnavailable as exc:
            raise OctopError(
                ErrorCode.NETWORK_ERROR, "business verification unavailable", status=503
            ) from exc
        except YouliClientError as exc:
            raise OctopError(ErrorCode.AUTH_FAILED, "business verification failed") from exc
        finally:
            self._services.youli_login_request_repo.consume(started.request_id)
        now = int(time.time())
        ttl = int(self._cfg.token_ttl_seconds)
        try:
            self._services.youli_identity_binding_repo.bind(
                user_id=user_id,
                provider_id=self.ensure_provider(),
                feishu_claim=feishu_claim,
                feishu_subject=feishu_claim["subject"],
                subject=f"youli:{issued.company_id}:{issued.employee_id}",
                sid=uuid.uuid4().hex,
                account_user_id=issued.user_id,
                account_username=issued.username,
                company_id=issued.company_id,
                employee_id=issued.employee_id,
                industry=issued.industry,
                token_blob=encrypt_secret(self._services.secret_repo, issued.token),
                token_expires_at=now + ttl,
                abs_expires_at=now + min(int(self._services.config.access_token_ttl_seconds), ttl),
            )
        except ValueError as exc:
            raise OctopError(ErrorCode.SSO_IDENTITY_TAKEN, "business identity conflict") from exc

    def binding_status(self, user_id: int) -> dict[str, Any]:
        row = self._services.youli_identity_binding_repo.get(user_id)
        if row is None:
            return {"bound": False, "delegation_active": False}
        stored_session = self._services.youli_session_repo.get(str(row["sid"]))
        if stored_session is None or stored_session.status == "revoked":
            return {"bound": False, "delegation_active": False}
        session = self._services.youli_session_repo.get_active(str(row["sid"]))
        return {
            "bound": True,
            "company_id": int(row["company_id"]),
            "employee_id": int(row["employee_id"]),
            "delegation_active": session is not None,
        }

    async def unlink_employee(self, user_id: int) -> None:
        provider = self._services.sso_repo.get_by_kind(YOULI_PROVIDER_KIND)
        self._services.youli_identity_binding_repo.unlink(user_id, provider.id if provider else 0)

    async def _verify_identity(self, issued: YouliIssuedToken) -> None:
        payload = await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._client.verify(issued.token)
        )
        failure = _admission_failure(
            payload,
            user_id=issued.user_id,
            company_id=issued.company_id,
            employee_id=issued.employee_id,
        )
        if failure:
            raise OctopError(ErrorCode.AUTH_FAILED, failure)

    async def _revoke_upstream(self, issued: YouliIssuedToken) -> None:
        try:
            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._client.revoke(
                    username=issued.username,
                    company_id=issued.company_id,
                    industry=issued.industry,
                ),
            )
        except Exception:  # noqa: BLE001 - compensation failure must not mask the original
            logger.exception("youli revoke after failed exchange did not complete")

    def session(self, sid: str) -> YouliSessionRow | None:
        return self._services.youli_session_repo.get_active(sid)

    async def logout(self, *, session: YouliSessionRow) -> None:
        self._services.youli_session_repo.revoke(session.sid)
        try:
            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._client.revoke(
                    username=session.account_username,
                    company_id=session.company_id,
                    industry=session.industry,
                ),
            )
        except Exception:  # noqa: BLE001
            logger.warning("youli upstream revoke failed for sid %s", session.sid)

    def _cookie_path(self) -> str:
        parsed = urlparse(self._cfg.callback_url)
        path = parsed.path or "/"
        if not path.endswith("/"):
            path = path.rsplit("/", 1)[0] + "/"
        exchange = "/api/auth/youli/exchange"
        common = _common_path_prefix(path, exchange)
        return common or "/"


def _common_path_prefix(left: str, right: str) -> str:
    left_parts = [p for p in left.split("/") if p]
    right_parts = [p for p in right.split("/") if p]
    shared: list[str] = []
    for a, b in zip(left_parts, right_parts, strict=False):
        if a != b:
            break
        shared.append(a)
    return "/" + "/".join(shared) if shared else ""


def _parse_company_expiration(raw: Any) -> datetime:
    text = str(raw).strip()
    if not text:
        raise ValueError("empty expiration")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if "T" in text:
        parsed = datetime.fromisoformat(text)
        return (
            parsed.replace(tzinfo=None)
            if parsed.tzinfo is None
            else parsed.astimezone().replace(tzinfo=None)
        )
    return datetime.fromisoformat(text[:19].replace(" ", "T"))


def _admission_failure(
    payload: dict[str, Any],
    *,
    user_id: int,
    company_id: int,
    employee_id: int,
) -> str | None:
    """Return why a verified business identity may not use Octop, if any."""
    if min(user_id, company_id, employee_id) <= 0:
        return "invalid business identity"
    if int(payload.get("userId") or 0) != user_id:
        return "handoff identity changed"
    if int(payload.get("companyId") or 0) != company_id:
        return "handoff company changed"
    if int(payload.get("employeeId") or 0) != employee_id:
        return "handoff employee changed"
    if int(payload.get("loginStatus") or 0) != 1:
        return "account is not allowed to sign in"
    if int(payload.get("mustChangePassword") or 0) == 1:
        return "password change required"
    expiration = payload.get("expiration")
    if not expiration:
        return "company expiration missing"
    try:
        expired = _parse_company_expiration(expiration) <= datetime.now()
    except ValueError:
        return "company expiration unreadable"
    if expired:
        return "company expired"
    return None


def revalidate_business_session(
    *, services: SharedServices, cfg: YouliConfig, session: YouliSessionRow, strict: bool = False
) -> None:
    """Re-check a delegated session's business token against MDM.

    Rejections revoke the sid so a stale delegation cannot outlive a company
    switch, password change or disabled account. Transport failures keep the
    session alive; the next request retries.
    """
    client = YouliClient(cfg)
    if not client.enabled or not cfg.mdm_base_url:
        if strict:
            raise OctopError(
                ErrorCode.NETWORK_ERROR, "business verification unavailable", status=503
            )
        return
    if session.token_blob is None:
        services.youli_session_repo.revoke(session.sid)
        raise OctopError(ErrorCode.TOKEN_EXPIRED, "business credential missing")
    try:
        token = decrypt_secret(services.secret_repo, session.token_blob)
    except Exception as exc:  # noqa: BLE001 - undecryptable credential is unusable
        services.youli_session_repo.revoke(session.sid)
        raise OctopError(ErrorCode.TOKEN_EXPIRED, "business credential unreadable") from exc
    try:
        payload = client.verify(token)
    except YouliClientUnavailable as exc:
        if strict:
            raise OctopError(
                ErrorCode.NETWORK_ERROR, "business verification unavailable", status=503
            ) from exc
        logger.warning("youli revalidation skipped: MDM unavailable for sid %s", session.sid)
        return
    except YouliClientError as exc:
        services.youli_session_repo.revoke(session.sid)
        reason = (
            resolve_youli_revocation_reason(
                services.youli_session_repo,
                sid=session.sid,
                user_id=session.user_id,
            )
            or REASON_UPSTREAM_EXPIRED
        )
        record_sid_revocation_reason(session.sid, reason)
        msg = _SESSION_REPLACED_MESSAGE if reason == REASON_SESSION_REPLACED else "请重新登录"
        raise OctopError(ErrorCode.TOKEN_EXPIRED, msg, details={"reason": reason}) from exc
    failure = _admission_failure(
        payload,
        user_id=session.account_user_id,
        company_id=session.company_id,
        employee_id=session.employee_id,
    )
    if failure:
        services.youli_session_repo.revoke(session.sid)
        record_sid_revocation_reason(session.sid, REASON_UPSTREAM_EXPIRED)
        raise OctopError(
            ErrorCode.TOKEN_EXPIRED,
            f"请重新登录：{failure}",
            details={"reason": REASON_UPSTREAM_EXPIRED},
        )
