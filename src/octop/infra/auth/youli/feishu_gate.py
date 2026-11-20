"""Feishu-to-Youli ERP phone verification gate and pending-bind state machine.

Kept separate from ``octop.infra.auth.sso.service`` so upstream SSO refactoring
stays isolated from HuiHuo ERP admission and binding rules.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Callable
from typing import Any, TypeVar
from urllib.parse import urlencode

from octop.infra.auth.sso.mobile_linking import extract_feishu_tenant, parse_allowed_tenants
from octop.infra.auth.sso.providers.feishu import mask_mobile, normalize_cn_mobile
from octop.infra.auth.youli.client import YouliClientError, YouliClientUnavailable
from octop.infra.auth.youli.service import YouliAuthService
from octop.infra.db.repos.sso import SsoLoginStateRow, SsoPendingBindRow, SsoProviderRow
from octop.infra.db.repos.users import UserRow
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.identity import User
from octop.infra.users.manager import UserManager

_PENDING_BIND_TTL_SECONDS = 600

logger = logging.getLogger(__name__)

_R = TypeVar("_R")


def _extract_legacy_feishu_subject(subject: str) -> str | None:
    """Return the bare legacy Feishu ID carried inside a ``feishu-v1:...`` subject."""
    if not subject.startswith("feishu-v1:"):
        return None
    try:
        parts = json.loads(subject.removeprefix("feishu-v1:"))
    except ValueError:
        return None
    if isinstance(parts, list) and len(parts) == 4 and isinstance(parts[3], str) and parts[3]:
        return parts[3]
    return None


class FeishuErpGate:
    """Enforces ERP account verification and guided binding on Feishu SSO logins."""

    def __init__(
        self,
        *,
        services: SharedServices,
        user_manager: UserManager,
        ws_hub: Any = None,
    ) -> None:
        self._services = services
        self._user_manager = user_manager
        self._ws_hub = ws_hub

    def youli_service(self) -> YouliAuthService:
        return YouliAuthService(
            services=self._services,
            users=self._user_manager,
            cfg=self._services.config.youli,
            ws_hub=self._ws_hub,
        )

    def _is_reclaimable_feishu_orphan(self, owner_id: int, provider_id: int) -> bool:
        """True only for legacy passwordless non-admin shells holding solely this Feishu link."""
        owner = self._services.user_repo.get(owner_id)
        if owner is None or owner.role == "admin" or owner.password_hash is not None:
            return False
        identities = self._services.user_repo.list_sso_identities(owner.id)
        return len(identities) == 1 and identities[0].provider_id == provider_id

    def _find_feishu_owner(self, provider_id: int, subject: str) -> tuple[UserRow | None, bool]:
        """Look up by scoped ``feishu-v1:...`` subject first, then fall back to legacy bare ID."""
        owner = self._services.user_repo.get_by_sso(provider_id, subject)
        if owner is not None:
            return owner, False
        legacy_raw = _extract_legacy_feishu_subject(subject)
        if legacy_raw is not None:
            legacy_owner = self._services.user_repo.get_by_sso(provider_id, legacy_raw)
            if legacy_owner is not None:
                return legacy_owner, True
        return None, False

    def _ensure_bindable(self, *, erp_user_id: int, provider_id: int, subject: str) -> None:
        owner, _ = self._find_feishu_owner(provider_id, subject)
        if owner is None or owner.id == erp_user_id:
            return
        if not self._is_reclaimable_feishu_orphan(owner.id, provider_id):
            raise OctopError(
                ErrorCode.SSO_IDENTITY_TAKEN,
                "this identity is already linked to another user",
            )

    async def bind_feishu_to_erp_user(
        self,
        *,
        erp_user_id: int,
        provider_id: int,
        subject: str,
        claims: dict[str, Any],
    ) -> User:
        """Bind a Feishu identity to an ERP user, reclaiming pure legacy Feishu shells only."""
        owner, _ = self._find_feishu_owner(provider_id, subject)
        if owner is not None and owner.id != erp_user_id:
            if self._is_reclaimable_feishu_orphan(owner.id, provider_id):
                self._services.user_repo.remove_sso_identity(owner.id, provider_id=provider_id)
                self._services.audit_repo.write(
                    actor=owner.username,
                    action="user.sso_rebind_orphan",
                    target=owner.username,
                )
            else:
                raise OctopError(
                    ErrorCode.SSO_IDENTITY_TAKEN,
                    "this identity is already linked to another user",
                )
        return await self._user_manager.bind_sso_identity(
            user_id=erp_user_id,
            provider_id=provider_id,
            subject=subject,
            claims=claims,
        )

    def _create_pending_bind_redirect(
        self,
        *,
        provider: SsoProviderRow,
        subject: str,
        claims: dict[str, Any],
        mobile_masked: str,
        reason: str,
        redirect_after: str,
        frontend: str,
        make_redirect: Callable[[str], _R],
    ) -> _R:
        login_code = secrets.token_urlsafe(32)
        ticket = secrets.token_urlsafe(32)
        now = int(time.time())
        display_name = str(claims.get("name") or "").strip()
        email = str(claims.get("email") or "").strip() or None
        self._services.sso_repo.create_pending_bind(
            ticket=ticket,
            login_code=login_code,
            provider_id=provider.id,
            provider_kind=provider.kind,
            subject=subject,
            display_name=display_name,
            email=email,
            mobile_masked=mobile_masked,
            reason=reason,
            expires_at=now + _PENDING_BIND_TTL_SECONDS,
        )
        params = {"code": login_code, "redirect": redirect_after}
        return make_redirect(f"{frontend}/login/oidc/complete#{urlencode(params)}")

    async def handle_callback(
        self,
        *,
        provider: SsoProviderRow,
        subject: str,
        claims: dict[str, Any],
        state: str,
        login_state: SsoLoginStateRow,
        frontend: str,
        issue_login_redirect: Callable[..., _R],
        error_redirect: Callable[[str, str], _R],
        make_redirect: Callable[[str], _R],
    ) -> _R:
        mobile = normalize_cn_mobile(claims.get("mobile"))
        mobile_masked = mask_mobile(mobile)
        youli_svc = self.youli_service()

        existing, is_legacy = self._find_feishu_owner(provider.id, subject)
        if existing is not None:
            if existing.disabled:
                return error_redirect(frontend, "disabled")
            identities = self._services.user_repo.list_sso_identities(existing.id)
            youli_identity = next((item for item in identities if item.kind == "youli"), None)
            if youli_identity is not None:
                parsed = YouliAuthService.parse_youli_subject(youli_identity.subject)
                if parsed is not None:
                    company_id, employee_id = parsed
                    try:
                        lookup, erp_user, sid = await youli_svc.resolve_feishu_erp_by_employee(
                            user_id=existing.id,
                            company_id=company_id,
                            employee_id=employee_id,
                        )
                    except (YouliClientUnavailable, YouliClientError):
                        logger.warning(
                            "Feishu linked ERP re-check failed for user_id=%s", existing.id
                        )
                        return error_redirect(frontend, "erp_unavailable")
                    except OctopError as exc:
                        if exc.code is ErrorCode.USER_DISABLED:
                            return error_redirect(frontend, "disabled")
                        return self._create_pending_bind_redirect(
                            provider=provider,
                            subject=subject,
                            claims=claims,
                            mobile_masked=mobile_masked,
                            reason="erp_disabled",
                            redirect_after=login_state.redirect_after,
                            frontend=frontend,
                            make_redirect=make_redirect,
                        )
                    if lookup.status in {"ok", "ok_no_token"} and erp_user is not None:
                        if is_legacy and erp_user.id == existing.id:
                            self._services.user_repo.upsert_sso_identity(
                                existing.id, provider_id=provider.id, subject=subject
                            )
                        return issue_login_redirect(
                            state=state,
                            user_id=erp_user.id,
                            redirect_after=login_state.redirect_after,
                            frontend=frontend,
                            youli_sid=sid,
                        )
                    return self._create_pending_bind_redirect(
                        provider=provider,
                        subject=subject,
                        claims=claims,
                        mobile_masked=mobile_masked,
                        reason=lookup.reason or "erp_disabled",
                        redirect_after=login_state.redirect_after,
                        frontend=frontend,
                        make_redirect=make_redirect,
                    )
            if existing.role == "admin" and existing.password_hash is not None:
                if not is_legacy:
                    return issue_login_redirect(
                        state=state,
                        user_id=existing.id,
                        redirect_after=login_state.redirect_after,
                        frontend=frontend,
                    )
                tenant = extract_feishu_tenant(subject)
                allowed = parse_allowed_tenants((provider.extra or {}).get("allowed_tenants"))
                if tenant and tenant in allowed:
                    self._services.user_repo.upsert_sso_identity(
                        existing.id, provider_id=provider.id, subject=subject
                    )
                    return issue_login_redirect(
                        state=state,
                        user_id=existing.id,
                        redirect_after=login_state.redirect_after,
                        frontend=frontend,
                    )

        if not mobile:
            return self._create_pending_bind_redirect(
                provider=provider,
                subject=subject,
                claims=claims,
                mobile_masked="",
                reason="feishu_no_mobile",
                redirect_after=login_state.redirect_after,
                frontend=frontend,
                make_redirect=make_redirect,
            )

        try:
            lookup, erp_user, sid = await youli_svc.resolve_feishu_erp_by_phone(phone=mobile)
        except (YouliClientUnavailable, YouliClientError):
            logger.warning("Feishu ERP mobile lookup failed (mobile_masked=%s)", mobile_masked)
            return error_redirect(frontend, "erp_unavailable")
        except OctopError as exc:
            if exc.code is ErrorCode.USER_DISABLED:
                return error_redirect(frontend, "disabled")
            return self._create_pending_bind_redirect(
                provider=provider,
                subject=subject,
                claims=claims,
                mobile_masked=mobile_masked,
                reason="erp_disabled",
                redirect_after=login_state.redirect_after,
                frontend=frontend,
                make_redirect=make_redirect,
            )

        if lookup.status in {"ok", "ok_no_token"} and erp_user is not None:
            try:
                await self.bind_feishu_to_erp_user(
                    erp_user_id=erp_user.id,
                    provider_id=provider.id,
                    subject=subject,
                    claims=claims,
                )
            except OctopError as exc:
                if exc.code is ErrorCode.USER_DISABLED:
                    return error_redirect(frontend, "disabled")
                if exc.code is ErrorCode.SSO_IDENTITY_TAKEN:
                    return error_redirect(frontend, "identity_taken")
                return error_redirect(frontend, "generic")
            return issue_login_redirect(
                state=state,
                user_id=erp_user.id,
                redirect_after=login_state.redirect_after,
                frontend=frontend,
                youli_sid=sid,
            )

        reason = lookup.reason or ("erp_not_found" if lookup.status == "not_found" else "erp_disabled")
        return self._create_pending_bind_redirect(
            provider=provider,
            subject=subject,
            claims=claims,
            mobile_masked=mobile_masked,
            reason=reason,
            redirect_after=login_state.redirect_after,
            frontend=frontend,
            make_redirect=make_redirect,
        )

    def take_pending_bind_by_login_code(self, login_code: str) -> dict[str, Any] | None:
        """Consume a pending-bind callback login code and return the bind challenge payload."""
        self._services.sso_repo.delete_expired()
        row = self._services.sso_repo.take_pending_bind_by_login_code(login_code)
        if row is None:
            return None
        provider = self._services.sso_repo.get_by_id(row.provider_id)
        provider_name = (
            provider.display_name.strip()
            if provider is not None and provider.display_name.strip()
            else "飞书"
        )
        return {
            "erp_bind_required": True,
            "bind_ticket": row.ticket,
            "reason": row.reason,
            "provider": row.provider_kind,
            "provider_name": provider_name,
            "display_name": row.display_name,
            "mobile_masked": row.mobile_masked,
            "expires_in": max(1, int(row.expires_at) - int(time.time())),
        }

    def get_pending_bind(self, ticket: str) -> SsoPendingBindRow | None:
        self._services.sso_repo.delete_expired()
        return self._services.sso_repo.get_active_pending_bind(ticket)

    async def complete_pending_bind(self, *, ticket: str, erp_user_id: int) -> SsoPendingBindRow:
        """Validate conflict, atomically consume the ticket, then bind Feishu to the ERP user."""
        row = self.get_pending_bind(ticket)
        if row is None:
            raise OctopError(ErrorCode.OIDC_BAD_REQUEST, "飞书绑定已过期，请重新点击飞书登录")
        self._ensure_bindable(
            erp_user_id=erp_user_id,
            provider_id=row.provider_id,
            subject=row.subject,
        )
        if not self._services.sso_repo.consume_pending_bind(ticket):
            raise OctopError(ErrorCode.OIDC_BAD_REQUEST, "飞书绑定凭证已被使用，请重新点击飞书登录")
        claims: dict[str, Any] = {}
        if row.display_name:
            claims["name"] = row.display_name
        if row.email:
            claims["email"] = row.email
        bound_user = await self.bind_feishu_to_erp_user(
            erp_user_id=erp_user_id,
            provider_id=row.provider_id,
            subject=row.subject,
            claims=claims,
        )
        self._services.audit_repo.write(
            actor=bound_user.username,
            action="auth.feishu_erp_bind",
            target=bound_user.username,
        )
        return row
