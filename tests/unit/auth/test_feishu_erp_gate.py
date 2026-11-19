"""Tests for the Feishu login ERP phone verification gate and binding flow."""

from __future__ import annotations

import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import Response

from octop.api.routers.auth_oidc import exchange_login_code_response
from octop.api.routers.auth_youli import YouliStaffLoginBody, staff_login
from octop.config import OctopConfig, YouliConfig
from octop.infra.auth.sso.crypto import encrypt_secret
from octop.infra.auth.sso.providers.feishu import FeishuAdapter
from octop.infra.auth.sso.service import SsoService
from octop.infra.auth.tokens import decode_token
from octop.infra.auth.youli.client import (
    YouliClient,
    YouliClientError,
    YouliClientUnavailable,
    YouliInnerLookupResult,
    YouliIssuedToken,
)
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.identity import Role
from octop.infra.users.manager import UserManager
from octop.infra.utils.paths import PathLayout


def _youli_config() -> OctopConfig:
    return OctopConfig(
        access_token_ttl_seconds=86400,
        youli=YouliConfig(
            enabled=True,
            base_url="https://youli.example",
            client_id="octop",
            client_secret="secret",
            audience="octop",
            callback_url="https://octop.example/aiwork/login/callback",
            business_entry_url="https://youli.example/octop-login",
            mdm_base_url="https://mdm.example",
            inner_api_key="inner-key",
            token_ttl_seconds=3600,
            cookie_secure=True,
        ),
    )


def _fragment_params(url: str) -> dict[str, str]:
    parsed = urlsplit(url)
    return {k: v[0] for k, v in parse_qs(parsed.fragment).items()}


def _query_params(url: str) -> dict[str, str]:
    parsed = urlsplit(url)
    return {k: v[0] for k, v in parse_qs(parsed.query).items()}


def _valid_verify_payload(*, user_id: int = 11, company_id: int = 21, employee_id: int = 31) -> dict:
    return {
        "userId": user_id,
        "companyId": company_id,
        "employeeId": employee_id,
        "loginStatus": 1,
        "mustChangePassword": 0,
        "expiration": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 86400)),
        "username": "alice",
        "industry": 7,
    }


@pytest.fixture
async def gate_env(tmp_path):
    paths = PathLayout(tmp_path / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=_youli_config())
    services.secret_repo.get_or_create("jwt", lambda: b"test-secret-32-bytes-long-000000")
    user_manager = UserManager(services)
    await user_manager.create(
        username="admin",
        password="AdminPassword123",
        role=Role.ADMIN,
    )
    sso_service = SsoService(services, user_manager)
    secret_enc = encrypt_secret(services.secret_repo, "feishu-secret")
    provider = services.sso_repo.upsert_by_kind(
        "feishu",
        enabled=True,
        display_name="飞书",
        issuer="",
        client_id="cli_feishu",
        client_secret_enc=secret_enc,
        scopes="",
        dashboard_origin=None,
        extra={"region": "feishu"},
    )
    server = SimpleNamespace(
        services=services,
        user_manager=user_manager,
        sso_service=sso_service,
        app_runtime=None,
    )
    return SimpleNamespace(
        services=services,
        user_manager=user_manager,
        sso_service=sso_service,
        provider=provider,
        server=server,
    )


def _start_feishu(sso_service: SsoService, *, bind_user_id: int | None = None) -> str:
    started = sso_service.start_login_for_kind(
        "feishu",
        redirect_after="/chat",
        public_base="https://octop.example",
        bind_user_id=bind_user_id,
    )
    return started["state"]


def _patch_feishu_claims(
    monkeypatch: pytest.MonkeyPatch,
    *,
    subject: str = "on_feishu_1",
    claims: dict | None = None,
) -> None:
    payload = claims if claims is not None else {"name": "Alice", "mobile": "+86 138-0000-1234"}
    monkeypatch.setattr(
        FeishuAdapter,
        "complete_login",
        lambda self, code, *, row, login_state, redirect_uri: (subject, dict(payload)),
    )


@pytest.mark.asyncio
async def test_feishu_phone_match_ok_binds_and_mints_sid_jwt(gate_env, monkeypatch) -> None:
    _patch_feishu_claims(monkeypatch)
    monkeypatch.setattr(
        YouliClient,
        "inner_login_by_phone",
        lambda self, *, phone, terminal=10: YouliInnerLookupResult(
            status="ok",
            user_id=11,
            company_id=21,
            employee_id=31,
            username="alice",
            nick_name="Alice",
            mobile=phone,
            issued=YouliIssuedToken(
                token="biz-token-1",
                username="alice",
                user_id=11,
                company_id=21,
                employee_id=31,
                industry=7,
                terminal=10,
            ),
        ),
    )
    monkeypatch.setattr(YouliClient, "verify", lambda self, token: _valid_verify_payload())

    state = _start_feishu(gate_env.sso_service)
    res = await gate_env.sso_service.handle_callback(
        code="auth-code",
        state=state,
        error=None,
        public_base="https://octop.example",
    )

    assert "/login/oidc/complete#" in res.url
    params = _fragment_params(res.url)
    assert "code" in params

    exchanged = await exchange_login_code_response(params["code"], gate_env.server)
    assert "erp_bind_required" not in exchanged
    claims = decode_token(gate_env.services.secret_repo.get("jwt"), exchanged["access_token"])
    assert claims["sid"]

    # Only admin + ERP user should exist; no duplicate standalone Feishu shell user.
    users = gate_env.services.user_repo.list(include_disabled=True)
    assert len(users) == 2
    erp_user = gate_env.services.user_repo.get_by_sso(gate_env.provider.id, "on_feishu_1")
    assert erp_user is not None
    identities = {item.kind for item in gate_env.services.user_repo.list_sso_identities(erp_user.id)}
    assert identities == {"youli", "feishu"}


@pytest.mark.asyncio
async def test_feishu_phone_match_fallback_ok_no_token_allows_login(gate_env, monkeypatch) -> None:
    _patch_feishu_claims(monkeypatch)
    monkeypatch.setattr(
        YouliClient,
        "inner_login_by_phone",
        lambda self, *, phone, terminal=10: YouliInnerLookupResult(
            status="ok_no_token",
            user_id=11,
            company_id=21,
            employee_id=31,
            username="alice",
            nick_name="Alice",
            mobile=phone,
        ),
    )

    state = _start_feishu(gate_env.sso_service)
    res = await gate_env.sso_service.handle_callback(
        code="auth-code",
        state=state,
        error=None,
        public_base="https://octop.example",
    )
    params = _fragment_params(res.url)
    exchanged = await exchange_login_code_response(params["code"], gate_env.server)
    assert exchanged["access_token"]
    assert len(gate_env.services.user_repo.list(include_disabled=True)) == 2


@pytest.mark.asyncio
async def test_feishu_phone_not_found_blocks_user_creation_and_returns_bind_challenge(
    gate_env, monkeypatch
) -> None:
    _patch_feishu_claims(
        monkeypatch,
        subject="on_outsider",
        claims={"name": "External Bob", "mobile": "+86 13911112222"},
    )
    monkeypatch.setattr(
        YouliClient,
        "inner_login_by_phone",
        lambda self, *, phone, terminal=10: YouliInnerLookupResult(
            status="not_found", reason="erp_not_found", message="未找到"
        ),
    )

    state = _start_feishu(gate_env.sso_service)
    res = await gate_env.sso_service.handle_callback(
        code="auth-code",
        state=state,
        error=None,
        public_base="https://octop.example",
    )
    params = _fragment_params(res.url)
    exchanged = await exchange_login_code_response(params["code"], gate_env.server)

    assert exchanged["erp_bind_required"] is True
    assert exchanged["reason"] == "erp_not_found"
    assert exchanged["display_name"] == "External Bob"
    assert exchanged["mobile_masked"] == "139****2222"
    assert exchanged["bind_ticket"]
    assert gate_env.sso_service.get_pending_bind(exchanged["bind_ticket"]) is not None
    # Still only admin exists — no shell user created!
    assert len(gate_env.services.user_repo.list(include_disabled=True)) == 1


@pytest.mark.asyncio
async def test_feishu_missing_mobile_blocks_with_feishu_no_mobile(gate_env, monkeypatch) -> None:
    _patch_feishu_claims(monkeypatch, subject="on_nomobile", claims={"name": "NoPhone"})

    state = _start_feishu(gate_env.sso_service)
    res = await gate_env.sso_service.handle_callback(
        code="auth-code",
        state=state,
        error=None,
        public_base="https://octop.example",
    )
    params = _fragment_params(res.url)
    exchanged = await exchange_login_code_response(params["code"], gate_env.server)
    assert exchanged["erp_bind_required"] is True
    assert exchanged["reason"] == "feishu_no_mobile"
    assert exchanged["mobile_masked"] == ""
    assert len(gate_env.services.user_repo.list(include_disabled=True)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "erp_ambiguous_phone",
        "erp_disabled",
        "erp_must_change_password",
        "erp_no_company",
        "erp_login_forbidden",
        "erp_company_expired",
    ],
)
async def test_feishu_erp_blocked_reasons_produce_pending_bind(
    gate_env, monkeypatch, reason: str
) -> None:
    _patch_feishu_claims(monkeypatch, subject=f"on_{reason}")
    monkeypatch.setattr(
        YouliClient,
        "inner_login_by_phone",
        lambda self, *, phone, terminal=10: YouliInnerLookupResult(
            status="blocked", reason=reason, message=f"blocked:{reason}"
        ),
    )

    state = _start_feishu(gate_env.sso_service)
    res = await gate_env.sso_service.handle_callback(
        code="auth-code",
        state=state,
        error=None,
        public_base="https://octop.example",
    )
    params = _fragment_params(res.url)
    exchanged = await exchange_login_code_response(params["code"], gate_env.server)
    assert exchanged["erp_bind_required"] is True
    assert exchanged["reason"] == reason


@pytest.mark.asyncio
async def test_feishu_mdm_unavailable_fails_closed(gate_env, monkeypatch) -> None:
    _patch_feishu_claims(monkeypatch)

    def _boom(self, *, phone: str, terminal: int = 10):
        raise YouliClientUnavailable("timeout")

    monkeypatch.setattr(YouliClient, "inner_login_by_phone", _boom)

    state = _start_feishu(gate_env.sso_service)
    res = await gate_env.sso_service.handle_callback(
        code="auth-code",
        state=state,
        error=None,
        public_base="https://octop.example",
    )
    assert _query_params(res.url)["oidc_error"] == "erp_unavailable"


@pytest.mark.asyncio
async def test_already_bound_erp_user_logs_in_even_when_feishu_phone_differs(
    gate_env, monkeypatch
) -> None:
    youli_svc = gate_env.sso_service._feishu_gate().youli_service()
    youli_provider_id = youli_svc.ensure_provider()
    erp_user = await gate_env.user_manager.resolve_or_create_sso_user(
        provider_id=youli_provider_id,
        subject="youli:21:31",
        claims={"name": "alice"},
    )
    await gate_env.user_manager.bind_sso_identity(
        user_id=erp_user.id,
        provider_id=gate_env.provider.id,
        subject="on_bound_user",
        claims={"name": "Alice"},
    )

    # Feishu has a different phone, so lookup by phone would miss if not checked by employee!
    _patch_feishu_claims(
        monkeypatch,
        subject="on_bound_user",
        claims={"name": "Alice", "mobile": "13999998888"},
    )
    called_employee: list[tuple[int, int]] = []

    def _by_emp(self, *, company_id: int, employee_id: int, terminal: int = 10):
        called_employee.append((company_id, employee_id))
        return YouliInnerLookupResult(
            status="ok",
            user_id=11,
            company_id=company_id,
            employee_id=employee_id,
            username="alice",
            issued=YouliIssuedToken(
                token="biz-token-emp",
                username="alice",
                user_id=11,
                company_id=company_id,
                employee_id=employee_id,
                industry=7,
                terminal=10,
            ),
        )

    monkeypatch.setattr(YouliClient, "inner_login_by_employee", _by_emp)
    monkeypatch.setattr(YouliClient, "verify", lambda self, token: _valid_verify_payload())

    state = _start_feishu(gate_env.sso_service)
    res = await gate_env.sso_service.handle_callback(
        code="auth-code",
        state=state,
        error=None,
        public_base="https://octop.example",
    )
    assert called_employee == [(21, 31)]
    exchanged = await exchange_login_code_response(
        _fragment_params(res.url)["code"], gate_env.server
    )
    assert exchanged["user"]["id"] == erp_user.id


@pytest.mark.asyncio
async def test_legacy_orphan_feishu_shell_is_blocked_and_safely_reclaimed_on_erp_bind(
    gate_env, monkeypatch
) -> None:
    orphan = await gate_env.user_manager.resolve_or_create_sso_user(
        provider_id=gate_env.provider.id,
        subject="on_legacy_shell",
        claims={"name": "Legacy Shell", "preferred_username": "legacy_shell"},
    )
    assert orphan is not None

    _patch_feishu_claims(
        monkeypatch,
        subject="on_legacy_shell",
        claims={"name": "Legacy Shell", "mobile": "13900001111"},
    )
    monkeypatch.setattr(
        YouliClient,
        "inner_login_by_phone",
        lambda self, *, phone, terminal=10: YouliInnerLookupResult(
            status="not_found", reason="erp_not_found"
        ),
    )

    state = _start_feishu(gate_env.sso_service)
    res = await gate_env.sso_service.handle_callback(
        code="auth-code",
        state=state,
        error=None,
        public_base="https://octop.example",
    )
    exchanged = await exchange_login_code_response(
        _fragment_params(res.url)["code"], gate_env.server
    )
    assert exchanged["erp_bind_required"] is True
    ticket = exchanged["bind_ticket"]

    # Staff logs in with ERP password + bind_ticket -> reclaims Feishu identity onto ERP user.
    monkeypatch.setattr(YouliClient, "register", lambda self, **kwargs: None)
    monkeypatch.setattr(YouliClient, "password_login", lambda self, **kwargs: "erp-token")
    monkeypatch.setattr(YouliClient, "issue_code", lambda self, **kwargs: "handoff-code")
    monkeypatch.setattr(
        YouliClient,
        "redeem",
        lambda self, **kwargs: YouliIssuedToken(
            token="biz-token-2",
            username="alice",
            user_id=11,
            company_id=21,
            employee_id=31,
            industry=7,
            terminal=10,
        ),
    )
    monkeypatch.setattr(YouliClient, "verify", lambda self, token: _valid_verify_payload())

    req = SimpleNamespace(base_url="https://octop.example/")
    staff_res = await staff_login(
        YouliStaffLoginBody(mobile="13800001234", password="erp-password", bind_ticket=ticket),
        req,  # type: ignore[arg-type]
        Response(),
        gate_env.server,
    )
    assert staff_res["bound_provider"] == "feishu"
    new_owner = gate_env.services.user_repo.get_by_sso(gate_env.provider.id, "on_legacy_shell")
    assert new_owner is not None
    assert new_owner.id != orphan.id
    assert gate_env.services.user_repo.list_sso_identities(orphan.id) == []


@pytest.mark.asyncio
async def test_local_admin_with_feishu_bound_remains_usable(gate_env, monkeypatch) -> None:
    admin = gate_env.user_manager.get("admin")
    assert admin is not None
    await gate_env.user_manager.bind_sso_identity(
        user_id=admin.id,
        provider_id=gate_env.provider.id,
        subject="on_admin",
        claims={"name": "Admin"},
    )
    _patch_feishu_claims(monkeypatch, subject="on_admin", claims={"name": "Admin"})

    state = _start_feishu(gate_env.sso_service)
    res = await gate_env.sso_service.handle_callback(
        code="auth-code",
        state=state,
        error=None,
        public_base="https://octop.example",
    )
    exchanged = await exchange_login_code_response(
        _fragment_params(res.url)["code"], gate_env.server
    )
    assert exchanged["user"]["id"] == admin.id


@pytest.mark.asyncio
async def test_staff_login_bind_ticket_retry_and_taken_rollback(gate_env, monkeypatch) -> None:
    _patch_feishu_claims(
        monkeypatch,
        subject="on_retry_user",
        claims={"name": "Bob", "mobile": "13700001111"},
    )
    monkeypatch.setattr(
        YouliClient,
        "inner_login_by_phone",
        lambda self, *, phone, terminal=10: YouliInnerLookupResult(
            status="not_found", reason="erp_not_found"
        ),
    )

    state = _start_feishu(gate_env.sso_service)
    res = await gate_env.sso_service.handle_callback(
        code="auth-code",
        state=state,
        error=None,
        public_base="https://octop.example",
    )
    ticket = (await exchange_login_code_response(_fragment_params(res.url)["code"], gate_env.server))[
        "bind_ticket"
    ]

    req = SimpleNamespace(base_url="https://octop.example/")
    monkeypatch.setattr(YouliClient, "register", lambda self, **kwargs: None)

    def _wrong_pw(self, **kwargs):
        raise YouliClientError("手机号或密码错误")

    monkeypatch.setattr(YouliClient, "password_login", _wrong_pw)
    with pytest.raises(OctopError) as exc_info:
        await staff_login(
            YouliStaffLoginBody(mobile="13800001234", password="wrong", bind_ticket=ticket),
            req,  # type: ignore[arg-type]
            Response(),
            gate_env.server,
        )
    assert exc_info.value.code is ErrorCode.AUTH_FAILED
    # Ticket remains active for retry.
    assert gate_env.sso_service.get_pending_bind(ticket) is not None

    # Now simulate identity already owned by another real ERP user -> session must revoke!
    youli_provider_id = gate_env.sso_service._feishu_gate().youli_service().ensure_provider()
    other_erp = await gate_env.user_manager.resolve_or_create_sso_user(
        provider_id=youli_provider_id,
        subject="youli:99:99",
        claims={"name": "other_erp"},
    )
    await gate_env.user_manager.bind_sso_identity(
        user_id=other_erp.id,
        provider_id=gate_env.provider.id,
        subject="on_retry_user",
        claims={"name": "Bob"},
    )

    revoked_calls: list[dict] = []
    monkeypatch.setattr(YouliClient, "password_login", lambda self, **kwargs: "erp-token")
    monkeypatch.setattr(YouliClient, "issue_code", lambda self, **kwargs: "handoff-code")
    monkeypatch.setattr(
        YouliClient,
        "redeem",
        lambda self, **kwargs: YouliIssuedToken(
            token="biz-token-conflict",
            username="alice",
            user_id=11,
            company_id=21,
            employee_id=31,
            industry=7,
            terminal=10,
        ),
    )
    monkeypatch.setattr(YouliClient, "verify", lambda self, token: _valid_verify_payload())
    monkeypatch.setattr(YouliClient, "revoke", lambda self, **kwargs: revoked_calls.append(kwargs))

    with pytest.raises(OctopError) as conflict_exc:
        await staff_login(
            YouliStaffLoginBody(mobile="13800001234", password="correct", bind_ticket=ticket),
            req,  # type: ignore[arg-type]
            Response(),
            gate_env.server,
        )
    assert conflict_exc.value.code is ErrorCode.SSO_IDENTITY_TAKEN
    assert len(revoked_calls) == 1
    assert gate_env.services.youli_session_repo.list_active_for_user(
        gate_env.services.user_repo.get_by_sso(youli_provider_id, "youli:21:31").id
    ) == []


@pytest.mark.asyncio
async def test_logged_in_user_oauth_bind_preserves_bind_fragment_param(
    gate_env, monkeypatch
) -> None:
    admin = gate_env.user_manager.get("admin")
    assert admin is not None
    _patch_feishu_claims(monkeypatch, subject="on_admin_link", claims={"name": "Admin"})

    state = _start_feishu(gate_env.sso_service, bind_user_id=admin.id)
    res = await gate_env.sso_service.handle_callback(
        code="auth-code",
        state=state,
        error=None,
        public_base="https://octop.example",
    )
    params = _fragment_params(res.url)
    assert params.get("bind") == "1"


@pytest.mark.asyncio
async def test_multi_sso_account_is_not_reclaimed_as_pure_feishu_orphan(
    gate_env, monkeypatch
) -> None:
    dingtalk_secret = encrypt_secret(gate_env.services.secret_repo, "ding-secret")
    ding_provider = gate_env.services.sso_repo.upsert_by_kind(
        "dingtalk",
        enabled=True,
        display_name="钉钉",
        issuer="",
        client_id="cli_ding",
        client_secret_enc=dingtalk_secret,
        scopes="",
        dashboard_origin=None,
        extra={},
    )
    multi_user = await gate_env.user_manager.resolve_or_create_sso_user(
        provider_id=gate_env.provider.id,
        subject="on_multi_sso",
        claims={"name": "Multi SSO", "preferred_username": "multi_sso"},
    )
    await gate_env.user_manager.bind_sso_identity(
        user_id=multi_user.id,
        provider_id=ding_provider.id,
        subject="ding_sub_1",
        claims={"name": "Multi SSO"},
    )

    youli_provider_id = gate_env.sso_service._feishu_gate().youli_service().ensure_provider()
    erp_user = await gate_env.user_manager.resolve_or_create_sso_user(
        provider_id=youli_provider_id,
        subject="youli:21:88",
        claims={"name": "erp_target"},
    )

    with pytest.raises(OctopError) as exc_info:
        await gate_env.sso_service._feishu_gate().bind_feishu_to_erp_user(
            erp_user_id=erp_user.id,
            provider_id=gate_env.provider.id,
            subject="on_multi_sso",
            claims={"name": "Multi SSO"},
        )
    assert exc_info.value.code is ErrorCode.SSO_IDENTITY_TAKEN
    assert (
        gate_env.services.user_repo.get_by_sso(gate_env.provider.id, "on_multi_sso").id
        == multi_user.id
    )
