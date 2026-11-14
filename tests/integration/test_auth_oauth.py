"""OAuth SSO HTTP route integration tests."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from octop.infra.auth.sso.service import RedirectResult, SsoService
from tests.support.auth import bearer, bootstrap_admin, login


@pytest.fixture
async def client(app_client):
    yield app_client


async def test_oauth_public_routes_and_oidc_kind_share_redirect(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, srv, home = client
    await bootstrap_admin(c, home)
    assert srv.user_manager is not None
    user = srv.user_manager.get("admin")
    assert user is not None

    monkeypatch.setattr(
        SsoService,
        "start_login_for_kind",
        lambda self, kind, *, redirect_after, public_base, bind_user_id=None: {
            "authorization_url": (
                "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
                f"?kind={kind}&redirect={redirect_after}&state=browser-state"
            ),
            "state": "browser-state",
        },
    )

    async def callback(self: SsoService, **_: Any) -> RedirectResult:
        return RedirectResult("http://testserver/login/oidc/complete#code=one-time")

    async def exchange(self: SsoService, code: str):
        assert code == "one-time"
        return user

    monkeypatch.setattr(SsoService, "handle_callback", callback)
    monkeypatch.setattr(SsoService, "exchange_login_code", exchange)

    status = await c.get("/api/auth/oauth/status")
    assert status.status_code == 200
    kinds = {item["kind"] for item in status.json()["providers"]}
    assert kinds == {"oidc", "feishu", "dingtalk", "wecom"}

    start = await c.post("/api/auth/oauth/start", json={"kind": "oidc", "redirect_after": "/chat"})
    assert start.status_code == 200
    assert start.cookies["octop_sso_state"] == "browser-state"
    assert "Path=/api/auth" in start.headers["set-cookie"]

    callback_response = await c.get(
        "/api/auth/oauth/callback", params={"code": "provider-code", "state": "browser-state"}
    )
    assert callback_response.status_code == 302
    assert callback_response.headers["location"].endswith("#code=one-time")

    via_oidc = await c.post("/api/auth/oidc/exchange", json={"code": "one-time"})
    assert via_oidc.status_code == 200
    assert via_oidc.json()["user"]["username"] == "admin"


async def test_oauth_bind_requires_auth_and_providers_are_not_public(client) -> None:
    c, _srv, home = client
    await bootstrap_admin(c, home)
    assert (await c.post("/api/auth/oauth/bind/start", json={"kind": "feishu"})).status_code == 401
    assert (await c.get("/api/auth/oauth/providers/feishu")).status_code == 401
    token = await login(c)
    started = await c.post(
        "/api/auth/oauth/bind/start",
        headers=bearer(token),
        json={"kind": "feishu"},
    )
    # Provider is not configured, so start is a 400 — but it is authenticated.
    assert started.status_code == 400

    for kind in ("feishu", "dingtalk", "wecom"):
        config = await c.get(f"/api/auth/oauth/providers/{kind}", headers=bearer(token))
        assert config.status_code == 200
        assert config.json()["kind"] == kind
        assert config.json()["redirect_uri"].endswith("/api/auth/oauth/callback")
        assert "client_secret" not in config.json()


async def test_oauth_callback_accepts_dingtalk_auth_code(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, srv, home = client
    await bootstrap_admin(c, home)

    seen: dict[str, str | None] = {}

    async def callback(self: SsoService, **kwargs: Any) -> RedirectResult:
        seen["code"] = kwargs.get("code")
        return RedirectResult("http://testserver/login/oidc/complete#code=one-time")

    monkeypatch.setattr(SsoService, "handle_callback", callback)
    monkeypatch.setattr(
        SsoService,
        "start_login_for_kind",
        lambda self, kind, *, redirect_after, public_base, bind_user_id=None: {
            "authorization_url": "https://login.dingtalk.com/oauth2/auth?state=browser-state",
            "state": "browser-state",
        },
    )

    start = await c.post(
        "/api/auth/oauth/start", json={"kind": "dingtalk", "redirect_after": "/chat"}
    )
    assert start.status_code == 200

    response = await c.get(
        "/api/auth/oauth/callback",
        params={"authCode": "ding-auth", "state": "browser-state"},
    )
    assert response.status_code == 302
    assert seen["code"] == "ding-auth"


@pytest.mark.parametrize("kind", ["dingtalk", "feishu", "wecom"])
async def test_oauth_config_and_authorization_preserve_forwarded_port(client, kind: str) -> None:
    c, _srv, home = client
    await bootstrap_admin(c, home)
    token = await login(c)
    proxy_headers = {
        "X-Forwarded-Proto": "https",
        "X-Forwarded-Host": "octop.example",
        "X-Forwarded-Port": "8011",
    }
    admin_headers = {**bearer(token), **proxy_headers}
    callback_uri = "https://octop.example:8011/api/auth/oauth/callback"

    saved = await c.put(
        f"/api/auth/oauth/providers/{kind}",
        headers=admin_headers,
        json={
            "enabled": True,
            "client_id": "test-app-id",
            "client_secret": "test-app-secret",
            "extra": {"agent_id": "1000001"} if kind == "wecom" else {},
        },
    )
    assert saved.status_code == 200
    assert saved.json()["redirect_uri"] == callback_uri

    config = await c.get(f"/api/auth/oauth/providers/{kind}", headers=admin_headers)
    assert config.status_code == 200
    assert config.json()["redirect_uri"] == callback_uri

    started = await c.post(
        "/api/auth/oauth/start",
        headers=proxy_headers,
        json={"kind": kind, "redirect_after": "/chat"},
    )
    assert started.status_code == 200
    query = parse_qs(urlsplit(started.json()["authorization_url"]).query)
    assert query["redirect_uri"] == [callback_uri]


async def test_oauth_import_channel_populates_feishu_and_dingtalk(client) -> None:
    import json

    c, srv, home = client
    await bootstrap_admin(c, home)
    token = await login(c)
    admin = srv.user_manager.get("admin")
    assert admin is not None

    empty_resp = await c.post(
        "/api/auth/oauth/providers/feishu/import-channel",
        headers=bearer(token),
    )
    assert empty_resp.status_code == 400

    unsupported_resp = await c.post(
        "/api/auth/oauth/providers/wecom/import-channel",
        headers=bearer(token),
    )
    assert unsupported_resp.status_code == 400

    srv.services.channel_repo.create(
        channel_id="ch-feishu-1",
        agent_id="main",
        user_id=admin.id,
        kind="feishu",
        name="feishu-bot",
        config_json=json.dumps({"app_id": "cli_feishu_1", "app_secret": "sec_feishu_1"}),
    )
    srv.services.channel_repo.create(
        channel_id="ch-dingtalk-1",
        agent_id="main",
        user_id=admin.id,
        kind="dingtalk",
        name="dingtalk-bot",
        config_json=json.dumps({"client_id": "ding_id_1", "client_secret": "ding_sec_1"}),
    )

    feishu_res = await c.post(
        "/api/auth/oauth/providers/feishu/import-channel",
        headers=bearer(token),
    )
    assert feishu_res.status_code == 200
    assert feishu_res.json()["enabled"] is True
    assert feishu_res.json()["client_id"] == "cli_feishu_1"
    assert feishu_res.json()["has_client_secret"] is True

    ding_res = await c.post(
        "/api/auth/oauth/providers/dingtalk/import-channel",
        headers=bearer(token),
    )
    assert ding_res.status_code == 200
    assert ding_res.json()["enabled"] is True
    assert ding_res.json()["client_id"] == "ding_id_1"
    assert ding_res.json()["has_client_secret"] is True

    status = await c.get("/api/auth/oauth/status")
    assert status.status_code == 200
    enabled_kinds = {
        item["kind"] for item in status.json()["providers"] if item["enabled"] is True
    }
    assert {"feishu", "dingtalk"}.issubset(enabled_kinds)


@pytest.mark.parametrize("kind", ["feishu", "dingtalk"])
async def test_feishu_and_dingtalk_full_login_callback_exchange_flow(
    client, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    import httpx

    c, _srv, home = client
    await bootstrap_admin(c, home)
    token = await login(c)

    saved = await c.put(
        f"/api/auth/oauth/providers/{kind}",
        headers=bearer(token),
        json={
            "enabled": True,
            "client_id": f"{kind}-client",
            "client_secret": f"{kind}-secret",
        },
    )
    assert saved.status_code == 200

    started = await c.post(
        "/api/auth/oauth/start",
        json={"kind": kind, "redirect_after": "/work/progress"},
    )
    assert started.status_code == 200
    state = started.json()["state"]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/open-apis/authen/v2/oauth/token"):
            return httpx.Response(200, json={"code": 0, "access_token": "fs-access"})
        if url.endswith("/open-apis/authen/v1/user_info"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "union_id": "on_feishu_union",
                        "name": "飞书员工",
                        "email": "feishu@example.com",
                        "mobile": "+8613800138000",
                    },
                },
            )
        if url.endswith("/v1.0/oauth2/userAccessToken"):
            return httpx.Response(200, json={"accessToken": "dt-access"})
        if url.endswith("/v1.0/contact/users/me"):
            return httpx.Response(
                200,
                json={
                    "unionId": "dt_union_1",
                    "nick": "钉钉员工",
                    "email": "ding@example.com",
                    "mobile": "13900139000",
                },
            )
        return httpx.Response(404, json={"error": f"unexpected {url}"})

    monkeypatch.setattr(
        SsoService,
        "_http_client",
        lambda self: httpx.Client(transport=httpx.MockTransport(handler)),
    )

    params = {"state": state}
    if kind == "dingtalk":
        params["authCode"] = "dt-auth-code"
    else:
        params["code"] = "fs-auth-code"

    callback = await c.get("/api/auth/oauth/callback", params=params)
    assert callback.status_code == 302
    fragment = urlsplit(callback.headers["location"]).fragment
    login_code = parse_qs(fragment)["code"][0]

    exchanged = await c.post("/api/auth/oauth/exchange", json={"code": login_code})
    assert exchanged.status_code == 200
    payload = exchanged.json()
    assert payload["access_token"]

    me = await c.get("/api/auth/me", headers=bearer(payload["access_token"]))
    assert me.status_code == 200
    assert kind in {b["kind"] for b in me.json()["sso_identities"]}


def test_feishu_and_dingtalk_to_claims_preserves_mobile() -> None:
    from octop.infra.auth.sso.providers.dingtalk import DingTalkAdapter
    from octop.infra.auth.sso.providers.feishu import FeishuAdapter

    assert FeishuAdapter.to_claims(
        {"name": "张三", "enterprise_email": "z@corp.cn", "mobile": " 13800138000 "}
    ) == {"name": "张三", "email": "z@corp.cn", "mobile": "13800138000"}
    assert DingTalkAdapter.to_claims(
        {"nick": "李四", "email": "l@corp.cn", "mobile": " 13900139000 "}
    ) == {"name": "李四", "email": "l@corp.cn", "mobile": "13900139000"}
