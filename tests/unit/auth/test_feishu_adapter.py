"""Tests for the Feishu dashboard SSO adapter."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import patch

import httpx
import jwt
import pytest

from octop.config import OctopConfig, YouliConfig
from octop.infra.auth.sso.crypto import encrypt_secret
from octop.infra.auth.sso.providers.feishu import (
    FeishuAdapter,
    mask_mobile,
    normalize_cn_mobile,
)
from octop.infra.auth.sso.service import SsoService
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.users.manager import UserManager
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def service(tmp_path):
    paths = PathLayout(tmp_path / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    return SsoService(services, UserManager(services))


def _feishu_row(service: SsoService, *, region: str = "feishu"):
    secret = encrypt_secret(service._services.secret_repo, "app-secret")
    return service._services.sso_repo.upsert_by_kind(
        "feishu",
        enabled=True,
        display_name="Feishu",
        issuer="",
        client_id="cli_xxx",
        client_secret_enc=secret,
        scopes="",
        dashboard_origin=None,
        extra={"region": region},
    )


def test_feishu_authorize_url_omits_scope_and_uses_region_hosts(service: SsoService) -> None:
    adapter = FeishuAdapter(service)
    row = _feishu_row(service, region="lark")
    url = adapter.authorize_url(
        row=row,
        state="st",
        nonce="ignored",
        code_challenge="challenge",
        redirect_uri="https://octop.example/api/auth/oauth/callback",
    )
    assert "accounts.larksuite.com" in url
    query = httpx.QueryParams(url.split("?", 1)[1])
    assert query["client_id"] == "cli_xxx"
    assert query["response_type"] == "code"
    assert query["code_challenge_method"] == "S256"
    assert "scope" not in query


def test_feishu_is_configured_without_issuer(service: SsoService) -> None:
    adapter = FeishuAdapter(service)
    row = _feishu_row(service)
    assert adapter.is_configured(row)
    with patch.object(service._user_manager, "count", return_value=1):
        status = service.providers_status()
    feishu = next(item for item in status["providers"] if item["kind"] == "feishu")
    assert feishu["enabled"] is True


@pytest.mark.parametrize(
    ("region", "accounts_host", "open_host"),
    [
        ("feishu", "accounts.feishu.cn", "open.feishu.cn"),
        ("lark", "accounts.larksuite.com", "open.larksuite.com"),
    ],
)
def test_feishu_complete_login_reads_v3_token_without_data_wrapper(
    service: SsoService, region: str, accounts_host: str, open_host: str
) -> None:
    """The v3 token endpoint can return the token at the top level."""
    adapter = FeishuAdapter(service)
    row = _feishu_row(service, region=region)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/v3/token":
            assert request.url.host == accounts_host
            body = json.loads(request.content)
            assert body["grant_type"] == "authorization_code"
            assert body["client_id"] == "cli_xxx"
            assert body["client_secret"] == "app-secret"
            assert body["code"] == "auth-code"
            assert body["code_verifier"] == "pkce"
            assert body["redirect_uri"] == "https://octop.example/api/auth/oauth/callback"
            return httpx.Response(
                200,
                json={
                    "access_token": "user-token",
                    "expires_in": 7200,
                    "token_type": "Bearer",
                },
            )
        assert request.url.host == open_host
        assert request.url.path == "/open-apis/authen/v1/user_info"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "ok",
                "data": {"union_id": "on_union", "tenant_key": "tenant-a", "name": "Ada"},
            },
        )

    login_state = type("State", (), {"code_verifier": "pkce"})()
    real_client = httpx.Client

    def factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs.pop("transport", None)
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    with patch("octop.infra.auth.sso.service.httpx.Client", side_effect=factory):
        subject, claims = adapter.complete_login(
            "auth-code",
            row=row,
            login_state=login_state,  # type: ignore[arg-type]
            redirect_uri="https://octop.example/api/auth/oauth/callback",
        )

    assert json.loads(subject.removeprefix("feishu-v1:")) == [
        region,
        "cli_xxx",
        "tenant-a",
        "on_union",
    ]
    assert claims["name"] == "Ada"


def test_feishu_complete_login_unwraps_envelope_and_falls_back_to_open_id(
    service: SsoService,
) -> None:
    adapter = FeishuAdapter(service)
    row = _feishu_row(service)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/oauth/v3/token":
            assert request.url.host == "accounts.feishu.cn"
            body = json.loads(request.content)
            assert body["grant_type"] == "authorization_code"
            assert body["code_verifier"] == "pkce"
            assert request.headers["content-type"].startswith("application/json")
            return httpx.Response(
                200,
                json={"code": 0, "msg": "ok", "data": {"access_token": "user-token"}},
            )
        assert request.url.path.endswith("/user_info")
        assert request.headers["authorization"] == "Bearer user-token"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "ok",
                "data": {
                    "open_id": "ou_open",
                    "tenant_key": "tenant-a",
                    "name": "Ada",
                    "enterprise_email": "ada@example.com",
                },
            },
        )

    login_state = type(
        "State",
        (),
        {"code_verifier": "pkce", "nonce": "n", "provider_id": row.id},
    )()
    real_client = httpx.Client

    def factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs.pop("transport", None)
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    with patch("octop.infra.auth.sso.service.httpx.Client", side_effect=factory):
        subject, claims = adapter.complete_login(
            "auth-code",
            row=row,
            login_state=login_state,  # type: ignore[arg-type]
            redirect_uri="https://octop.example/api/auth/oauth/callback",
        )

    assert json.loads(subject.removeprefix("feishu-v1:")) == [
        "feishu",
        "cli_xxx",
        "tenant-a",
        "ou_open",
    ]
    assert claims["name"] == "Ada"
    assert claims["email"] == "ada@example.com"
    assert requests[0].method == "POST"


def test_feishu_token_envelope_error_is_value_error(service: SsoService) -> None:
    adapter = FeishuAdapter(service)
    row = _feishu_row(service)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 20027, "msg": "invalid scope", "data": {}})

    real_client = httpx.Client

    def factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs.pop("transport", None)
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    login_state = type("State", (), {"code_verifier": "pkce"})()
    with (
        patch("octop.infra.auth.sso.service.httpx.Client", side_effect=factory),
        pytest.raises(ValueError, match="invalid scope"),
    ):
        adapter.complete_login(
            "auth-code",
            row=row,
            login_state=login_state,  # type: ignore[arg-type]
            redirect_uri="https://octop.example/api/auth/oauth/callback",
        )


def test_feishu_missing_subject_is_invalid_token(service: SsoService) -> None:
    adapter = FeishuAdapter(service)
    row = _feishu_row(service)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/v3/token":
            return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"access_token": "t"}})
        return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"name": "Ada"}})

    real_client = httpx.Client

    def factory(*args: object, **kwargs: object) -> httpx.Client:
        kwargs.pop("transport", None)
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    login_state = type("State", (), {"code_verifier": "pkce"})()
    with (
        patch("octop.infra.auth.sso.service.httpx.Client", side_effect=factory),
        pytest.raises(jwt.InvalidTokenError),
    ):
        adapter.complete_login(
            "auth-code",
            row=row,
            login_state=login_state,  # type: ignore[arg-type]
            redirect_uri="https://octop.example/api/auth/oauth/callback",
        )


def test_normalize_and_mask_mobile() -> None:
    assert normalize_cn_mobile("13800001234") == "13800001234"
    assert normalize_cn_mobile("+86-138 0000 1234") == "13800001234"
    assert normalize_cn_mobile("8613800001234") == "13800001234"
    assert normalize_cn_mobile("") == ""
    assert normalize_cn_mobile(None) == ""
    assert normalize_cn_mobile("abc") == ""
    assert mask_mobile("+86 138-0000-1234") == "138****1234"
    assert mask_mobile("123456") == ""
    assert mask_mobile(None) == ""


def test_feishu_authorize_url_requests_phone_scope_when_youli_enabled(tmp_path) -> None:
    paths = PathLayout(tmp_path / ".octop-youli")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    cfg = OctopConfig(youli=YouliConfig(enabled=True))
    services = build_shared_services(db=db, paths=paths, config=cfg)
    svc = SsoService(services, UserManager(services))
    adapter = FeishuAdapter(svc)
    row = _feishu_row(svc, region="feishu")
    url = adapter.authorize_url(
        row=row,
        state="st",
        nonce="ignored",
        code_challenge="challenge",
        redirect_uri="https://octop.example/api/auth/oauth/callback",
    )
    query = httpx.QueryParams(url.split("?", 1)[1])
    assert query["scope"] == "contact:user.phone:readonly"


@pytest.mark.asyncio
async def test_legacy_feishu_identity_requires_authenticated_rebinding(service, monkeypatch):
    row = _feishu_row(service)
    users = service._services.user_repo
    owner = users.create(username="owner", password_hash=None, role="user")
    other = users.create(username="other", password_hash=None, role="user")
    users.upsert_sso_identity(owner, provider_id=row.id, subject="old-union")
    subject = 'feishu-v1:["feishu","cli_xxx","tenant-a","old-union"]'
    monkeypatch.setattr(FeishuAdapter, "complete_login", lambda *args, **kwargs: (subject, {}))

    async def callback(user_id):
        start = service.start_login_for_kind(
            "feishu", redirect_after=None, public_base="https://octop.example", bind_user_id=user_id
        )
        return await service.handle_callback(
            code="code", state=start["state"], error=None, public_base="https://octop.example"
        )

    assert "legacy_feishu_recovery" in (await callback(None)).url
    assert "identity_taken" in (await callback(other)).url
    assert users.get_by_sso(row.id, "old-union").id == owner
    assert "#code=" in (await callback(owner)).url
    assert users.get_by_sso(row.id, subject).id == owner
    assert users.get_by_sso(row.id, "old-union") is None


@pytest.mark.asyncio
async def test_existing_scoped_feishu_identity_cannot_be_replaced(service, monkeypatch):
    row = _feishu_row(service)
    users = service._services.user_repo
    owner = users.create(username="owner", password_hash=None, role="user")
    original = 'feishu-v1:["feishu","cli_xxx","tenant-a","subject"]'
    users.upsert_sso_identity(owner, provider_id=row.id, subject=original)
    new_tenant = 'feishu-v1:["feishu","cli_xxx","tenant-b","subject"]'
    monkeypatch.setattr(FeishuAdapter, "complete_login", lambda *args, **kwargs: (new_tenant, {}))
    start = service.start_login_for_kind(
        "feishu", redirect_after=None, public_base="https://octop.example", bind_user_id=owner
    )
    result = await service.handle_callback(
        code="code", state=start["state"], error=None, public_base="https://octop.example"
    )
    assert "identity_taken" in result.url
    assert users.get_by_sso(row.id, original).id == owner
    assert users.get_by_sso(row.id, new_tenant) is None


@pytest.mark.asyncio
async def test_erp_deployment_requires_existing_account_for_first_feishu_login(service, monkeypatch):
    row = _feishu_row(service)
    config = service._services.config
    services = replace(
        service._services, config=replace(config, youli=replace(config.youli, enabled=True))
    )
    service = SsoService(services, UserManager(services))
    owner = services.user_repo.create(username="erp-user", password_hash=None, role="user")
    subject = 'feishu-v1:["feishu","cli_xxx","tenant-a","subject"]'
    monkeypatch.setattr(FeishuAdapter, "complete_login", lambda *_args, **_kwargs: (subject, {}))

    async def callback(user_id=None):
        start = service.start_login_for_kind(
            "feishu", redirect_after=None, public_base="https://octop.example", bind_user_id=user_id
        )
        return await service.handle_callback(
            code="code", state=start["state"], error=None, public_base="https://octop.example"
        )

    assert "binding_required" in (await callback()).url
    assert services.user_repo.count() == 1
    assert "#code=" in (await callback(owner)).url
    assert "#code=" in (await callback()).url
    assert services.user_repo.get_by_sso(row.id, subject).id == owner
