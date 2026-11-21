"""End-to-end (mocked upstream) tests for the Youli handoff service."""

from __future__ import annotations

import time

import pytest

from octop.config import OctopConfig, YouliConfig
from octop.infra.auth.sso.crypto import decrypt_secret
from octop.infra.auth.tokens import decode_token
from octop.infra.auth.youli.client import (
    YouliClient,
    YouliClientError,
    YouliClientUnavailable,
    YouliIssuedToken,
)
from octop.infra.auth.youli.service import YouliAuthService, revalidate_business_session
from octop.infra.connectors.service import ConnectorService
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.manager import UserManager
from octop.infra.utils.paths import PathLayout


def _config() -> OctopConfig:
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
            token_ttl_seconds=3600,
            cookie_secure=True,
        ),
    )


@pytest.fixture
def service(tmp_path):
    paths = PathLayout(tmp_path / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=_config())
    services.secret_repo.get_or_create("jwt", lambda: b"test-secret-32-bytes-long-000000")
    return YouliAuthService(
        services=services, users=UserManager(services), cfg=services.config.youli
    )


def _mock_client(monkeypatch):
    monkeypatch.setattr(YouliClient, "register", lambda self, **kwargs: None)
    monkeypatch.setattr(
        YouliClient,
        "redeem",
        lambda self, **kwargs: YouliIssuedToken(
            token="business-token",
            username="alice",
            user_id=11,
            company_id=21,
            employee_id=31,
            industry=7,
            terminal=10,
        ),
    )
    monkeypatch.setattr(
        YouliClient,
        "verify",
        lambda self, token: {
            "userId": 11,
            "companyId": 21,
            "employeeId": 31,
            "loginStatus": 1,
            "mustChangePassword": 0,
            "expiration": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 86400)),
            "username": "alice",
            "industry": 7,
        },
    )


@pytest.mark.asyncio
async def test_start_and_exchange_creates_session(service, monkeypatch):
    _mock_client(monkeypatch)
    started = await service.start(public_base="https://octop.example")
    assert started.entry_url.startswith("https://youli.example/octop-login?")
    assert started.cookie_name.endswith(started.request_id)

    result = await service.exchange(
        code="code-1",
        state=started.state,
        request_id=started.request_id,
        browser_token=started.browser_token,
    )
    payload = decode_token(
        service._services.secret_repo.get("jwt"),
        result.access_token,  # noqa: SLF001
    )
    assert payload["sid"] == result.session.sid
    assert payload["purpose"] == "access"
    assert payload["mfa_version"] == 0
    assert payload["password_version"] == 0
    assert result.user.username
    assert service.session(result.session.sid) is not None


@pytest.mark.asyncio
async def test_exchange_provisions_one_personal_mcn_connector(service, monkeypatch):
    _mock_client(monkeypatch)
    started = await service.start(public_base="https://octop.example")
    result = await service.exchange(
        code="code-1",
        state=started.state,
        request_id=started.request_id,
        browser_token=started.browser_token,
    )

    def mcn_rows():
        return [
            row
            for row in service._services.repos.connector_repo.list_by_user(result.user.id)
            if row.kind == "mcn"
        ]

    rows = mcn_rows()
    assert len(rows) == 1
    assert rows[0].has_credentials
    creds = ConnectorService(
        repo=service._services.repos.connector_repo,
        secret_repo=service._services.secret_repo,
        settings_repo=service._services.settings_repo,
        config=service._services.config,
        youli_session_repo=service._services.youli_session_repo,
    ).decrypt(rows[0].instance_id)
    assert creds["token"] == "business-token"
    assert creds["internal_token"]

    service._ensure_personal_mcn_connector(user=result.user, token="business-token")
    assert [row.instance_id for row in mcn_rows()] == [rows[0].instance_id]


@pytest.mark.asyncio
async def test_exchange_rejects_wrong_browser(service, monkeypatch):
    _mock_client(monkeypatch)
    started = await service.start(public_base="https://octop.example")
    with pytest.raises(OctopError):
        await service.exchange(
            code="code-1",
            state=started.state,
            request_id=started.request_id,
            browser_token="not-the-cookie",
        )


@pytest.mark.asyncio
async def test_exchange_is_single_use(service, monkeypatch):
    _mock_client(monkeypatch)
    started = await service.start(public_base="https://octop.example")
    await service.exchange(
        code="code-1",
        state=started.state,
        request_id=started.request_id,
        browser_token=started.browser_token,
    )
    with pytest.raises(OctopError):
        await service.exchange(
            code="code-1",
            state=started.state,
            request_id=started.request_id,
            browser_token=started.browser_token,
        )


@pytest.mark.asyncio
async def test_identity_change_revokes_upstream(service, monkeypatch):
    _mock_client(monkeypatch)
    revoked: list[dict] = []

    def wrong_identity(self, token):
        return {
            "userId": 99,
            "companyId": 21,
            "employeeId": 31,
            "loginStatus": 1,
            "mustChangePassword": 0,
            "expiration": "2099-01-01 00:00:00",
        }

    monkeypatch.setattr(YouliClient, "verify", wrong_identity)
    monkeypatch.setattr(YouliClient, "revoke", lambda self, **kwargs: revoked.append(kwargs))
    started = await service.start(public_base="https://octop.example")
    with pytest.raises(OctopError):
        await service.exchange(
            code="code-1",
            state=started.state,
            request_id=started.request_id,
            browser_token=started.browser_token,
        )
    assert revoked and revoked[0]["username"] == "alice"


async def _active_session(service, monkeypatch):
    _mock_client(monkeypatch)
    started = await service.start(public_base="https://octop.example")
    return await service.exchange(
        code="code-1",
        state=started.state,
        request_id=started.request_id,
        browser_token=started.browser_token,
    )


def _verified(company_id: int = 21) -> dict:
    return {
        "userId": 11,
        "companyId": company_id,
        "employeeId": 31,
        "loginStatus": 1,
        "mustChangePassword": 0,
        "expiration": "2099-01-01 00:00:00",
    }


@pytest.mark.asyncio
async def test_revalidation_revokes_after_company_switch(service, monkeypatch):
    result = await _active_session(service, monkeypatch)
    monkeypatch.setattr(YouliClient, "verify", lambda self, token: _verified(22))

    with pytest.raises(OctopError) as caught:
        revalidate_business_session(
            services=service._services,  # noqa: SLF001
            cfg=service._cfg,  # noqa: SLF001
            session=result.session,
        )

    assert caught.value.code is ErrorCode.TOKEN_EXPIRED
    assert service.session(result.session.sid) is None


@pytest.mark.asyncio
async def test_revalidation_revokes_when_mdm_rejects(service, monkeypatch):
    result = await _active_session(service, monkeypatch)

    def reject(self, token):
        raise YouliClientError("youli MDM rejected the token: 401 请重新登录")

    monkeypatch.setattr(YouliClient, "verify", reject)

    with pytest.raises(OctopError):
        revalidate_business_session(
            services=service._services,  # noqa: SLF001
            cfg=service._cfg,  # noqa: SLF001
            session=result.session,
        )

    assert service.session(result.session.sid) is None


@pytest.mark.asyncio
async def test_revalidation_keeps_session_when_mdm_is_unreachable(service, monkeypatch):
    result = await _active_session(service, monkeypatch)

    def unavailable(self, token):
        raise YouliClientUnavailable("youli MDM unreachable")

    monkeypatch.setattr(YouliClient, "verify", unavailable)

    revalidate_business_session(
        services=service._services,  # noqa: SLF001
        cfg=service._cfg,  # noqa: SLF001
        session=result.session,
    )

    assert service.session(result.session.sid) is not None


@pytest.mark.asyncio
async def test_new_login_revokes_previous_sid_and_pushes_ws_notice(service, monkeypatch):
    pushed: list[tuple[str, dict]] = []

    class FakeHub:
        async def push_to_sid(self, sid: str, frame: dict) -> None:
            pushed.append((sid, frame))

    service._ws_hub = FakeHub()  # noqa: SLF001
    first = await _active_session(service, monkeypatch)
    second = await _active_session(service, monkeypatch)

    assert first.session.sid != second.session.sid
    assert service.session(first.session.sid) is None
    assert service.session(second.session.sid) is not None
    assert len(pushed) == 1
    assert pushed[0][0] == first.session.sid
    assert pushed[0][1]["type"] == "session_revoked"
    assert pushed[0][1]["reason"] == "session_replaced"

    from octop.infra.auth.youli.service import (
        _REVOKED_SID_REASONS,
        REASON_SESSION_REPLACED,
        resolve_youli_revocation_reason,
    )

    assert (
        resolve_youli_revocation_reason(
            service._services.youli_session_repo,  # noqa: SLF001
            sid=first.session.sid,
            user_id=first.user.id,
        )
        == REASON_SESSION_REPLACED
    )
    # Survives in-memory cache loss (e.g. restart) by checking newer active sessions.
    _REVOKED_SID_REASONS.pop(first.session.sid, None)
    assert (
        resolve_youli_revocation_reason(
            service._services.youli_session_repo,  # noqa: SLF001
            sid=first.session.sid,
            user_id=first.user.id,
        )
        == REASON_SESSION_REPLACED
    )


def _feishu_user(service, username: str) -> int:
    services = service._services
    user_id = services.user_repo.create(username=username, password_hash=None, role="user")
    provider = services.sso_repo.upsert_by_kind(
        "feishu",
        enabled=True,
        display_name="Feishu",
        issuer="",
        client_id="app",
        client_secret_enc=None,
        scopes="",
        dashboard_origin=None,
        extra={},
    )
    services.user_repo.upsert_sso_identity(
        user_id,
        provider_id=provider.id,
        subject=f'feishu-v1:["feishu","app","tenant","{username}"]',
    )
    return user_id


def _mock_password(monkeypatch):
    def issue_delegation(self, *, mobile, password):
        if (mobile, password) != ("123456", "correct"):
            raise YouliClientError("wrong credentials")
        return YouliIssuedToken(
            token="delegated-token",
            username="alice",
            user_id=11,
            company_id=21,
            employee_id=31,
            industry=7,
            terminal=1_000_000_001,
        )

    monkeypatch.setattr(YouliClient, "issue_delegation", issue_delegation)


def _claim(service, user_id, generation=None):
    from octop.infra.db.repos.feishu_erp_reauth import config_fingerprint

    identity = next(
        i for i in service._services.user_repo.list_sso_identities(user_id) if i.kind == "feishu"
    )
    provider = service._services.sso_repo.get_by_id(identity.provider_id)
    return {
        "user_id": user_id,
        "provider_id": provider.id,
        "subject": identity.subject,
        "client_id": provider.client_id,
        "region": "feishu",
        "config_fingerprint": config_fingerprint(vars(provider)),
        "generation": service._services.youli_identity_binding_repo.begin_attempt(user_id)
        if generation is None
        else generation,
    }


def _stored_binding(service, user_id, sid="binding", generation=None):
    repo = service._services.youli_identity_binding_repo
    feishu = next(
        i for i in service._services.user_repo.list_sso_identities(user_id) if i.kind == "feishu"
    )
    expires = int(time.time()) + 3600
    repo.bind(
        user_id=user_id,
        provider_id=service.ensure_provider(),
        subject="youli:21:31",
        feishu_claim=_claim(service, user_id, generation),
        feishu_subject=feishu.subject,
        sid=sid,
        account_user_id=11,
        account_username="alice",
        company_id=21,
        employee_id=31,
        industry=7,
        token_blob=b"encrypted",
        token_expires_at=expires,
        abs_expires_at=expires,
    )


@pytest.mark.parametrize("unlink_feishu", [False, True])
def test_binding_quarantine_and_unlink_transaction(service, unlink_feishu):
    user_id = _feishu_user(service, "legacy-sync")
    users = service._services.user_repo
    sessions = service._services.youli_session_repo
    provider_id = service.ensure_provider()
    users.upsert_sso_identity(user_id, provider_id=provider_id, subject="youli:21:31")
    _stored_binding(service, user_id)
    with service._services.db.transaction() as conn:
        conn.execute("UPDATE youli_identity_binding SET had_independent_identity = NULL WHERE user_id = ?", (user_id,))
    expires = int(time.time()) + 3600
    for sid, employee in (("old-login", 31), ("other-employee", 32)):
        sessions.create(
            sid=sid,
            user_id=user_id,
            account_user_id=11,
            account_username="alice",
            company_id=21,
            employee_id=employee,
            industry=7,
            token_blob=b"encrypted",
            token_expires_at=expires,
            abs_expires_at=expires,
        )
    assert sessions.get_active("old-login") is None
    if unlink_feishu:
        feishu = service._services.sso_repo.get_by_kind("feishu")
        users.remove_sso_identity(user_id, provider_id=feishu.id)
    else:
        service._services.youli_identity_binding_repo.unlink(user_id, provider_id)
    assert sessions.get("binding").token_blob is None
    assert sessions.get("old-login").token_blob is None
    assert sessions.get("other-employee").token_blob == b"encrypted"
    assert users.get_by_sso(provider_id, "youli:21:31").id == user_id
    assert not service.binding_status(user_id)["bound"]
    assert service._services.youli_identity_binding_repo.is_reserved(21, 31)
    with pytest.raises(ValueError, match="reserved"):
        sessions.create_replacing(
            sid="racing",
            user_id=user_id,
            account_user_id=11,
            account_username="alice",
            company_id=21,
            employee_id=31,
            industry=7,
            token_blob=b"encrypted",
            token_expires_at=expires,
            abs_expires_at=expires,
        )


def test_binding_rejects_existing_identity_and_other_binding(service):
    first = _feishu_user(service, "first-sync")
    second = _feishu_user(service, "second-sync")
    _stored_binding(service, first)
    with pytest.raises(ValueError, match="already linked"):
        _stored_binding(service, second, sid="second")
    assert service._services.youli_session_repo.get_active("binding") is not None


def test_binding_rejects_existing_erp_identity(service):
    owner = _feishu_user(service, "existing-owner")
    claimant = _feishu_user(service, "existing-claimant")
    provider = service.ensure_provider()
    service._services.user_repo.upsert_sso_identity(
        owner, provider_id=provider, subject="youli:21:31"
    )
    with pytest.raises(ValueError, match="already linked"):
        _stored_binding(service, claimant)


@pytest.mark.asyncio
async def test_exchange_rejects_reserved_employee_before_resolving_user(service, monkeypatch):
    user_id = _feishu_user(service, "reserved-login")
    _stored_binding(service, user_id)

    async def redeemed(**_kwargs):
        return YouliIssuedToken(
            token="token",
            username="alice",
            user_id=11,
            company_id=21,
            employee_id=31,
            industry=7,
            terminal=10,
        )

    monkeypatch.setattr(service, "_redeem_verified", redeemed)
    with pytest.raises(OctopError) as error:
        await service.exchange(
            code="code", state="state", request_id="request", browser_token="browser"
        )
    assert error.value.code is ErrorCode.AUTH_FAILED
    assert service._services.user_repo.get_by_sso(service.ensure_provider(), "youli:21:31") is None


@pytest.mark.parametrize("unlink_feishu", [False, True])
def test_binding_generation_blocks_unlink_race(service, unlink_feishu):
    user_id = _feishu_user(service, "race-sync")
    repo = service._services.youli_identity_binding_repo
    generation = repo.begin_attempt(user_id)
    if unlink_feishu:
        feishu = service._services.sso_repo.get_by_kind("feishu")
        service._services.user_repo.remove_sso_identity(user_id, provider_id=feishu.id)
    else:
        repo.unlink(user_id, service.ensure_provider())
    if unlink_feishu:
        # Restore the identity: generation must still reject the stale attempt.
        service._services.user_repo.upsert_sso_identity(
            user_id,
            provider_id=feishu.id,
            subject='feishu-v1:["feishu","app","tenant","race-sync"]',
        )
    with pytest.raises(ValueError, match="superseded"):
        _stored_binding(service, user_id, generation=generation)


@pytest.mark.asyncio
async def test_bind_requires_both_accounts_and_unlink_erases_delegation(service, monkeypatch):
    _mock_client(monkeypatch)
    _mock_password(monkeypatch)
    user_id = _feishu_user(service, "feishu-user")
    other_id = _feishu_user(service, "other")
    with pytest.raises(OctopError):
        await service.bind_employee(
            user_id=user_id,
            feishu_claim=_claim(service, user_id),
            mobile="123456",
            password="wrong",
        )
    assert not service.binding_status(user_id)["bound"]
    await service.bind_employee(
        user_id=user_id,
        feishu_claim=_claim(service, user_id),
        mobile="123456",
        password="correct",
    )
    binding = service._services.youli_identity_binding_repo.get(user_id)
    sid = binding["sid"]
    assert all(i.kind != "youli" for i in service._services.user_repo.list_sso_identities(user_id))
    assert service._services.user_repo.get(user_id).sso_provider_id != service.ensure_provider()
    assert service.binding_status(user_id)["delegation_active"]
    stored_token = service._services.youli_session_repo.get(sid).token_blob
    assert stored_token is not None and stored_token != b"delegated-token"
    assert decrypt_secret(service._services.secret_repo, stored_token) == "delegated-token"
    assert not service.binding_status(other_id)["bound"]
    await service.unlink_employee(other_id)
    assert service._services.youli_session_repo.get_active(sid) is not None
    await service.unlink_employee(user_id)
    assert not service.binding_status(user_id)["bound"]
    assert service._services.youli_session_repo.get(sid).token_blob is None
    assert service._services.youli_session_repo.get_active(sid) is None


@pytest.mark.asyncio
async def test_bind_rejects_other_owner_and_mismatched_reauthorization(service, monkeypatch):
    _mock_client(monkeypatch)
    _mock_password(monkeypatch)
    one = _feishu_user(service, "one")
    two = _feishu_user(service, "two")
    await service.bind_employee(
        user_id=one,
        feishu_claim=_claim(service, one),
        mobile="123456",
        password="correct",
    )
    sid = service._services.youli_identity_binding_repo.get(one)["sid"]
    with pytest.raises(OctopError):
        await service.bind_employee(
            user_id=two,
            feishu_claim=_claim(service, two),
            mobile="123456",
            password="correct",
        )
    assert service._services.youli_session_repo.get_active(sid) is not None
    monkeypatch.setattr(
        YouliClient,
        "issue_delegation",
        lambda self, **kwargs: YouliIssuedToken(
            token="different",
            username="alice",
            user_id=11,
            company_id=22,
            employee_id=31,
            industry=7,
            terminal=1_000_000_002,
        ),
    )
    monkeypatch.setattr(YouliClient, "verify", lambda self, token: _verified(22))
    with pytest.raises(OctopError):
        await service.bind_employee(
            user_id=one,
            feishu_claim=_claim(service, one),
            mobile="123456",
            password="correct",
        )
    assert service._services.youli_session_repo.get_active(sid) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["company", "employee", "disabled", "unavailable"])
async def test_binding_fails_closed_on_mdm_rejection(service, monkeypatch, change):
    _mock_client(monkeypatch)
    _mock_password(monkeypatch)
    user_id = _feishu_user(service, "employee")

    def verify(self, token):
        if change == "unavailable":
            raise YouliClientUnavailable("unavailable")
        payload = _verified()
        payload[
            {"company": "companyId", "employee": "employeeId", "disabled": "loginStatus"}[change]
        ] = 0
        return payload

    monkeypatch.setattr(YouliClient, "verify", verify)
    with pytest.raises(OctopError):
        await service.bind_employee(
            user_id=user_id,
            feishu_claim=_claim(service, user_id),
            mobile="123456",
            password="correct",
        )
    assert not service.binding_status(user_id)["bound"]
    assert not service._services.youli_session_repo.list_active_for_user(user_id)


@pytest.mark.asyncio
async def test_unlink_invalidates_inflight_binding(service, monkeypatch):
    _mock_client(monkeypatch)
    _mock_password(monkeypatch)
    user_id = _feishu_user(service, "employee")
    original = service._verify_identity

    async def verify_then_unlink(issued):
        await original(issued)
        await service.unlink_employee(user_id)

    monkeypatch.setattr(service, "_verify_identity", verify_then_unlink)
    with pytest.raises(OctopError):
        await service.bind_employee(
            user_id=user_id,
            feishu_claim=_claim(service, user_id),
            mobile="123456",
            password="correct",
        )
    assert not service.binding_status(user_id)["bound"]


@pytest.mark.asyncio
async def test_binding_api_is_authenticated_and_scoped(service, monkeypatch):
    from types import SimpleNamespace

    import httpx
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from octop.api.deps import current_user, get_server, is_jwt_exempt_path
    from octop.api.routers.auth_youli import router
    from octop.infra.users.identity import User

    _mock_client(monkeypatch)
    _mock_password(monkeypatch)
    first = _feishu_user(service, "first")
    second = _feishu_user(service, "second")
    app = FastAPI()
    app.include_router(router, prefix="/api/auth/youli")
    server = SimpleNamespace(services=service._services, user_manager=service._users)

    async def test_server():
        return server

    app.dependency_overrides[get_server] = test_server

    @app.exception_handler(OctopError)
    async def error_handler(request, exc):
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    assert not is_jwt_exempt_path("/api/auth/youli/binding")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        unauthenticated = await client.post(
            "/api/auth/youli/binding", json={"mobile": "123456", "password": "correct"}
        )
        assert unauthenticated.status_code == 401

        async def first_user():
            return User(id=first, username="first", role="user", display_name=None)

        app.dependency_overrides[current_user] = first_user
        bound = await client.post(
            "/api/auth/youli/binding", json={"mobile": "123456", "password": "correct"}
        )
        assert bound.status_code == 422
        assert any(error["loc"][-1] == "proof" for error in bound.json()["detail"])
        assert not service.binding_status(first)["bound"]

        # Existing delegations can still be inspected and unlinked.
        _stored_binding(service, first)

        async def second_user():
            return User(id=second, username="second", role="user", display_name=None)

        app.dependency_overrides[current_user] = second_user
        assert (await client.get("/api/auth/youli/binding")).json()["bound"] is False
        assert (await client.delete("/api/auth/youli/binding")).status_code == 200
        assert service.binding_status(first)["delegation_active"] is True


@pytest.mark.asyncio
async def test_changed_feishu_identity_never_activates_old_delegation(service, monkeypatch):
    _mock_client(monkeypatch)
    _mock_password(monkeypatch)
    user_id = _feishu_user(service, "first")
    await service.bind_employee(
        user_id=user_id,
        feishu_claim=_claim(service, user_id),
        mobile="123456",
        password="correct",
    )
    sid = service._services.youli_identity_binding_repo.get(user_id)["sid"]
    provider = service._services.sso_repo.get_by_kind("feishu")
    service._services.user_repo.upsert_sso_identity(
        user_id, provider_id=provider.id, subject='feishu-v1:["feishu","app","other","first"]'
    )
    assert service._services.youli_session_repo.get_active(sid) is None
    with pytest.raises(OctopError):
        await service.bind_employee(
            user_id=user_id,
            feishu_claim=_claim(service, user_id),
            mobile="123456",
            password="correct",
        )


@pytest.mark.asyncio
async def test_regular_access_token_never_calls_mdm_or_attaches_delegation(service, monkeypatch):
    from types import SimpleNamespace

    from octop.api.deps import async_resolve_user_from_token
    from octop.infra.auth.tokens import PURPOSE_ACCESS, sign_token
    from octop.infra.auth.youli.context import current_youli_sid

    _mock_client(monkeypatch)
    _mock_password(monkeypatch)
    user_id = _feishu_user(service, "employee")
    await service._users.boot()
    await service.bind_employee(
        user_id=user_id,
        feishu_claim=_claim(service, user_id),
        mobile="123456",
        password="correct",
    )
    sid = service._services.youli_identity_binding_repo.get(user_id)["sid"]
    server = SimpleNamespace(services=service._services, user_manager=service._users)
    jwt_token = sign_token(
        service._services.secret_repo.get("jwt"),
        sub=user_id,
        uname="employee",
        role="user",
        purpose=PURPOSE_ACCESS,
    )
    def unexpected_verify(self, token):
        pytest.fail("ordinary Octop authentication must not verify an ERP delegation")

    monkeypatch.setattr(YouliClient, "verify", unexpected_verify)
    assert (await async_resolve_user_from_token(server, jwt_token)).id == user_id
    assert current_youli_sid() is None
    assert service._services.youli_session_repo.delegated_sid_for_user(user_id) == sid
    await service.unlink_employee(user_id)
    assert (await async_resolve_user_from_token(server, jwt_token)).id == user_id
    assert current_youli_sid() is None
    assert service._services.youli_session_repo.delegated_sid_for_user(user_id) is None


@pytest.mark.asyncio
async def test_reauthorization_replaces_session_and_expiration_disables_it(service, monkeypatch):
    _mock_client(monkeypatch)
    _mock_password(monkeypatch)
    user_id = _feishu_user(service, "employee")
    await service.bind_employee(
        user_id=user_id,
        feishu_claim=_claim(service, user_id),
        mobile="123456",
        password="correct",
    )
    first = service._services.youli_identity_binding_repo.get(user_id)["sid"]
    await service.bind_employee(
        user_id=user_id,
        feishu_claim=_claim(service, user_id),
        mobile="123456",
        password="correct",
    )
    second = service._services.youli_identity_binding_repo.get(user_id)["sid"]
    assert first != second
    assert service._services.youli_session_repo.get(first).token_blob is None
    assert service._services.youli_session_repo.get_active(second) is not None
    with service._services.db.transaction() as conn:
        conn.execute(
            "UPDATE youli_session SET token_expires_at = ? WHERE sid = ?",
            (int(time.time()) - 1, second),
        )
    assert service.binding_status(user_id)["delegation_active"] is False
    service._services.youli_session_repo.revoke(second)
    assert service.binding_status(user_id) == {
        "bound": True,
        "company_id": 21,
        "employee_id": 31,
        "delegation_active": False,
    }
    await service.unlink_employee(user_id)
    assert service.binding_status(user_id)["bound"] is False


@pytest.mark.asyncio
async def test_cross_user_sid_claim_is_rejected(service, monkeypatch):
    from types import SimpleNamespace

    from octop.api.deps import async_resolve_user_from_token
    from octop.infra.auth.tokens import PURPOSE_ACCESS, sign_token

    _mock_client(monkeypatch)
    _mock_password(monkeypatch)
    owner = _feishu_user(service, "owner")
    other = _feishu_user(service, "other")
    await service._users.boot()
    await service.bind_employee(
        user_id=owner,
        feishu_claim=_claim(service, owner),
        mobile="123456",
        password="correct",
    )
    sid = service._services.youli_identity_binding_repo.get(owner)["sid"]
    token = sign_token(
        service._services.secret_repo.get("jwt"),
        sub=other,
        uname="other",
        role="user",
        purpose=PURPOSE_ACCESS,
        sid=sid,
    )
    server = SimpleNamespace(services=service._services, user_manager=service._users)
    with pytest.raises(OctopError):
        await async_resolve_user_from_token(server, token)


@pytest.mark.asyncio
async def test_existing_youli_login_identity_cannot_be_claimed(service, monkeypatch):
    existing = await _active_session(service, monkeypatch)
    _mock_password(monkeypatch)

    def unexpected_login(*args, **kwargs):
        pytest.fail("the binding flow must not use the independent ERP login terminal")

    monkeypatch.setattr(YouliClient, "password_login", unexpected_login)
    monkeypatch.setattr(YouliClient, "redeem", unexpected_login)
    claimant = _feishu_user(service, "claimant")
    with pytest.raises(OctopError) as caught:
        await service.bind_employee(
            user_id=claimant,
            feishu_claim=_claim(service, claimant),
            mobile="123456",
            password="correct",
        )
    assert caught.value.code is ErrorCode.SSO_IDENTITY_TAKEN
    assert service._services.youli_session_repo.get_active(existing.session.sid) is not None
    assert not service.binding_status(claimant)["bound"]


@pytest.mark.asyncio
async def test_feishu_unlink_permanently_revokes_delegation(service, monkeypatch):
    _mock_client(monkeypatch)
    _mock_password(monkeypatch)
    user_id = _feishu_user(service, "employee")
    await service.bind_employee(
        user_id=user_id,
        feishu_claim=_claim(service, user_id),
        mobile="123456",
        password="correct",
    )
    sid = service._services.youli_identity_binding_repo.get(user_id)["sid"]
    provider = service._services.sso_repo.get_by_kind("feishu")
    identity = next(
        i for i in service._services.user_repo.list_sso_identities(user_id) if i.kind == "feishu"
    )
    service._services.user_repo.remove_sso_identity(user_id, provider_id=provider.id)
    assert service._services.youli_session_repo.get(sid).token_blob is None
    service._services.user_repo.upsert_sso_identity(
        user_id, provider_id=provider.id, subject=identity.subject
    )
    assert service._services.youli_session_repo.get_active(sid) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("unlink_feishu", [False, True])
async def test_unlink_quarantines_legacy_mapping_and_matching_sessions(
    service, monkeypatch, unlink_feishu
):
    _mock_client(monkeypatch)
    _mock_password(monkeypatch)
    user_id = _feishu_user(service, "legacy")
    provider = service.ensure_provider()
    service._services.user_repo.upsert_sso_identity(
        user_id, provider_id=provider, subject="youli:21:31"
    )
    await service.bind_employee(
        user_id=user_id,
        feishu_claim=_claim(service, user_id),
        mobile="123456",
        password="correct",
    )
    with service._services.db.transaction() as conn:
        conn.execute("UPDATE youli_identity_binding SET had_independent_identity = NULL WHERE user_id = ?", (user_id,))
    repo = service._services.youli_session_repo
    original = repo.get(service._services.youli_identity_binding_repo.get(user_id)["sid"])
    for sid, employee in (("old-login", 31), ("other-employee", 32)):
        repo.create(
            sid=sid,
            user_id=user_id,
            account_user_id=11,
            account_username="alice",
            company_id=21,
            employee_id=employee,
            industry=7,
            token_blob=b"encrypted",
            token_expires_at=original.token_expires_at,
            abs_expires_at=original.abs_expires_at,
        )
    if unlink_feishu:
        feishu = service._services.sso_repo.get_by_kind("feishu")
        service._services.user_repo.remove_sso_identity(user_id, provider_id=feishu.id)
    else:
        await service.unlink_employee(user_id)
    assert repo.get("old-login").token_blob is None
    assert repo.get("other-employee").token_blob == b"encrypted"
    assert service._services.user_repo.get_by_sso(provider, "youli:21:31").id == user_id
    assert not service.binding_status(user_id)["bound"]
    started = await service.start(public_base="https://octop.example")
    with pytest.raises(OctopError) as error:
        await service.exchange(
            code="code",
            state=started.state,
            request_id=started.request_id,
            browser_token=started.browser_token,
        )
    assert error.value.code is ErrorCode.AUTH_FAILED
    with pytest.raises(ValueError, match="reserved"):
        repo.create_replacing(
            sid="racing-login",
            user_id=user_id,
            account_user_id=11,
            account_username="alice",
            company_id=21,
            employee_id=31,
            industry=7,
            token_blob=b"encrypted",
            token_expires_at=original.token_expires_at,
            abs_expires_at=original.abs_expires_at,
        )


@pytest.mark.asyncio
async def test_feishu_unlink_invalidates_inflight_binding(service, monkeypatch):
    _mock_client(monkeypatch)
    _mock_password(monkeypatch)
    user_id = _feishu_user(service, "employee")
    original = service._verify_identity

    async def verify_then_unlink(issued):
        await original(issued)
        provider = service._services.sso_repo.get_by_kind("feishu")
        service._services.user_repo.remove_sso_identity(user_id, provider_id=provider.id)

    monkeypatch.setattr(service, "_verify_identity", verify_then_unlink)
    with pytest.raises(OctopError):
        await service.bind_employee(
            user_id=user_id,
            feishu_claim=_claim(service, user_id),
            mobile="123456",
            password="correct",
        )
    assert not service.binding_status(user_id)["bound"]


@pytest.mark.asyncio
async def test_unconfigured_mdm_rejects_before_issuing_delegation(service, monkeypatch):
    _mock_client(monkeypatch)
    _mock_password(monkeypatch)
    user_id = _feishu_user(service, "employee")
    from dataclasses import replace

    service._cfg = replace(service._cfg, mdm_base_url="")
    called = []
    monkeypatch.setattr(YouliClient, "issue_delegation", lambda *args, **kwargs: called.append(True))
    with pytest.raises(OctopError):
        await service.bind_employee(
            user_id=user_id,
            feishu_claim=_claim(service, user_id),
            mobile="123456",
            password="correct",
        )
    assert called == []
    assert not service.binding_status(user_id)["bound"]


@pytest.mark.asyncio
async def test_independent_login_survives_binding_and_unlink(service, monkeypatch):
    _mock_client(monkeypatch)
    user_id = _feishu_user(service, "independent")
    provider = service.ensure_provider()
    service._services.user_repo.upsert_sso_identity(
        user_id, provider_id=provider, subject="youli:21:31"
    )
    _stored_binding(service, user_id)
    assert service._services.youli_identity_binding_repo.get(user_id)["had_independent_identity"] == 1
    result = await _active_session(service, monkeypatch)
    assert result.user.id == user_id
    assert service.binding_status(user_id)["delegation_active"] is True
    await service.unlink_employee(user_id)
    assert service._services.youli_session_repo.get_active(result.session.sid) is not None
    assert service._services.user_repo.get_by_sso(provider, "youli:21:31").id == user_id
    result = await _active_session(service, monkeypatch)
    assert result.user.id == user_id


def test_delegation_unlink_allows_reassignment_but_rejects_conflicts(service):
    first = _feishu_user(service, "reassign-first")
    second = _feishu_user(service, "reassign-second")
    provider = service.ensure_provider()
    repo = service._services.youli_identity_binding_repo
    _stored_binding(service, first)
    assert repo.get(first)["had_independent_identity"] == 0
    assert service._services.user_repo.get_by_sso(provider, "youli:21:31") is None
    with pytest.raises(ValueError, match="already linked"):
        _stored_binding(service, second, sid="conflict")
    repo.unlink(first, provider)
    assert not repo.is_reserved(21, 31)
    _stored_binding(service, second, sid="reassigned")
    assert service._services.user_repo.get_by_sso(provider, "youli:21:31") is None
    assert repo.get(second)["employee_id"] == 31


def test_employee_login_and_binding_serialize_across_sqlite_connections(service):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from octop.infra.db.repos.youli import YouliIdentityBindingRepo, YouliSessionRepo

    user_id = _feishu_user(service, "race")
    provider = service.ensure_provider()
    claim = _claim(service, user_id)
    other_db = SqlitePool(service._services.db.path)
    barrier = Barrier(2)
    expires = int(time.time()) + 3600
    values = dict(account_user_id=11, account_username="alice", company_id=21,
                  employee_id=31, industry=7, token_blob=b"encrypted",
                  token_expires_at=expires, abs_expires_at=expires)

    def login():
        barrier.wait()
        try:
            return YouliSessionRepo(other_db).login(
                provider_id=provider, subject="youli:21:31", role="youli_employee",
                role_name="employee", permissions=[], sid="race-login", **values,
            )[0].id
        except ValueError:
            return None

    def bind():
        barrier.wait()
        try:
            YouliIdentityBindingRepo(service._services.db).bind(
                user_id=user_id, provider_id=provider, subject="youli:21:31",
                feishu_claim=claim, feishu_subject=claim["subject"], sid="race-bind", **values,
            )
            return True
        except ValueError:
            return False

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            login_result = executor.submit(login)
            bind_result = executor.submit(bind)
            owner, bound = login_result.result(timeout=10), bind_result.result(timeout=10)
        assert (owner is None) != (not bound)
        if owner is not None:
            assert owner != user_id
            assert service._services.user_repo.get_by_sso(provider, "youli:21:31").id == owner
        else:
            assert service._services.user_repo.get_by_sso(provider, "youli:21:31") is None
    finally:
        other_db.close()


def test_postgres_employee_lock_uses_stable_transaction_advisory_key():
    from octop.infra.db.repos.youli import lock_employee

    class Connection:
        calls = []

        def execute(self, sql, args):
            self.calls.append((sql, args))

    conn = Connection()
    lock_employee(conn, "postgresql", 21, 31)
    lock_employee(conn, "postgresql", 21, 31)
    assert conn.calls[0] == conn.calls[1]
    assert conn.calls[0][0] == "SELECT pg_advisory_xact_lock(?)"


@pytest.mark.asyncio
async def test_login_after_delegation_unlink_creates_separate_account(service, monkeypatch):
    first = _feishu_user(service, "former-delegate")
    provider = service.ensure_provider()
    _stored_binding(service, first)
    await service.unlink_employee(first)
    result = await _active_session(service, monkeypatch)
    assert result.user.id != first
    assert service._services.user_repo.get_by_sso(provider, "youli:21:31").id == result.user.id
    second = _feishu_user(service, "new-delegate")
    with pytest.raises(ValueError, match="already linked"):
        _stored_binding(service, second, sid="conflicting-delegation")
