"""End-to-end (mocked upstream) tests for the Youli handoff service."""

from __future__ import annotations

import time

import pytest

from octop.config import OctopConfig, YouliConfig
from octop.infra.auth.tokens import decode_token
from octop.infra.auth.youli.client import (
    YouliClient,
    YouliClientError,
    YouliClientUnavailable,
    YouliIssuedToken,
)
from octop.infra.auth.youli.service import YouliAuthService, revalidate_business_session
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
