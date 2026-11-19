"""Fresh Feishu ERP proof using real storage and the SSO HTTP adapter."""

from __future__ import annotations

import json
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from octop.config import OctopConfig
from octop.infra.auth.sso.crypto import encrypt_secret
from octop.infra.auth.sso.service import SsoService
from octop.infra.auth.youli.reauth import FeishuErpReauth, digest
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.errors import OctopError
from octop.infra.users.manager import UserManager
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    paths = PathLayout(tmp_path / "octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    uid = services.user_repo.create(username="person", password_hash=None, role="user")
    other = services.user_repo.create(username="other", password_hash=None, role="user")
    provider = services.sso_repo.upsert_by_kind(
        "feishu",
        enabled=True,
        display_name="Feishu",
        issuer="",
        client_id="app",
        client_secret_enc=encrypt_secret(services.secret_repo, "secret"),
        scopes="",
        dashboard_origin="https://example.test",
        extra={},
    )
    subject = 'feishu-v1:["feishu","app","tenant","person"]'
    services.user_repo.upsert_sso_identity(uid, provider_id=provider.id, subject=subject)
    remote = {"tenant": "tenant", "person": "person", "fail": False, "during": None}

    def handler(request):
        if request.url.path.endswith("/token"):
            assert json.loads(request.content)["code_verifier"]
            if remote["during"]:
                remote["during"]()
            if remote["fail"]:
                return httpx.Response(400, json={})
            return httpx.Response(200, json={"access_token": "remote-token"})
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"tenant_key": remote["tenant"], "union_id": remote["person"]},
            },
        )

    sso = SsoService(services, UserManager(services))
    monkeypatch.setattr(
        sso, "_http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    reauth = FeishuErpReauth(services, sso)
    yield services, reauth, uid, other, provider, remote
    db.close()


def start(ctx):
    services, auth, uid, _, provider, _ = ctx
    url = auth.start(uid, "session", provider.id)
    query = parse_qs(urlparse(url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["prompt"] == ["consent"]
    assert "secret" not in url and "session" not in url
    return query["state"][0]


def proof(ctx):
    _, auth, uid, _, _, _ = ctx
    return auth.callback(uid, "session", start(ctx), "code")


def test_owner_session_and_one_shot(ctx):
    services, auth, uid, other, provider, _ = ctx
    state = start(ctx)
    for user, session in [(other, "session"), (uid, "wrong")]:
        with pytest.raises(OctopError):
            auth.callback(user, session, state, "code")
    token = auth.callback(uid, "session", state, "code")
    with services.db.connect() as conn:
        row = conn.execute("SELECT * FROM feishu_erp_reauth_proof").fetchone()
        assert row["proof_hash"] == digest(token) and row["session_hash"] == digest("session")
    for user, session in [(other, "session"), (uid, "wrong")]:
        with pytest.raises(OctopError):
            auth.claim(user, session, token)
    claim = auth.claim(uid, "session", token)
    assert claim["subject"].endswith('"person"]')
    with pytest.raises(OctopError):
        auth.claim(uid, "session", token)
    with pytest.raises(OctopError):
        auth.callback(uid, "session", state, "code")


@pytest.mark.parametrize("field", ["tenant", "person"])
def test_mismatch_consumes_state(ctx, field):
    _, auth, uid, _, _, remote = ctx
    state = start(ctx)
    remote[field] = "wrong"
    with pytest.raises(OctopError):
        auth.callback(uid, "session", state, "code")
    remote[field] = "tenant" if field == "tenant" else "person"
    with pytest.raises(OctopError):
        auth.callback(uid, "session", state, "code")


def test_expiry_failure_and_wrong_provider(ctx):
    services, auth, uid, _, _, remote = ctx
    with pytest.raises(OctopError):
        auth.start(uid, "session", 999999)
    state = start(ctx)
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE feishu_erp_reauth_state SET expires_at=? WHERE state_hash=?",
            (int(time.time()), digest(state)),
        )
    with pytest.raises(OctopError):
        auth.callback(uid, "session", state, "code")
    state = start(ctx)
    remote["fail"] = True
    with pytest.raises(OctopError):
        auth.callback(uid, "session", state, "code")
    remote["fail"] = False
    with pytest.raises(OctopError):
        auth.callback(uid, "session", state, "code")
    token = proof(ctx)
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE feishu_erp_reauth_proof SET expires_at=? WHERE proof_hash=?",
            (int(time.time()), digest(token)),
        )
    with pytest.raises(OctopError):
        auth.claim(uid, "session", token)


def test_unlink_and_identity_change_fence_callback_and_claim(ctx):
    services, auth, uid, _, provider, remote = ctx
    state = start(ctx)
    remote["during"] = lambda: services.youli_identity_binding_repo.unlink(uid, 0)
    with pytest.raises(OctopError):
        auth.callback(uid, "session", state, "code")
    remote["during"] = None
    token = proof(ctx)
    services.user_repo.remove_sso_identity(uid, provider_id=provider.id)
    services.user_repo.upsert_sso_identity(
        uid, provider_id=provider.id, subject='feishu-v1:["feishu","app","tenant","person"]'
    )
    with pytest.raises(OctopError):
        auth.claim(uid, "session", token)


def test_simultaneous_claim_only_once(ctx):
    _, auth, uid, _, _, _ = ctx
    token = proof(ctx)
    barrier = threading.Barrier(3)
    outcomes = []

    def attempt():
        barrier.wait(timeout=3)
        try:
            auth.claim(uid, "session", token)
            outcomes.append("ok")
        except OctopError:
            outcomes.append("denied")

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=3)
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["denied", "ok"]


def test_simultaneous_callback_only_once(ctx):
    _, auth, uid, _, _, _ = ctx
    state = start(ctx)
    barrier = threading.Barrier(3)
    outcomes = []

    def attempt():
        barrier.wait(timeout=3)
        try:
            auth.callback(uid, "session", state, "code")
            outcomes.append("ok")
        except OctopError:
            outcomes.append("denied")

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=3)
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["denied", "ok"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["unlink", "identity", "disabled", "client", "secret", "region"]
)
async def test_claim_to_commit_fence(ctx, monkeypatch, mutation):
    from dataclasses import replace

    from tests.unit.youli.test_youli_service import _mock_client, _mock_password

    from octop.config import YouliConfig
    from octop.infra.auth.youli.service import YouliAuthService

    services, auth, uid, _, provider, _ = ctx
    claim = auth.claim(uid, "session", proof(ctx))
    config = YouliConfig(
        enabled=True,
        base_url="https://youli.example",
        client_id="octop",
        client_secret="secret",
        audience="octop",
        callback_url="https://octop.example/callback",
        business_entry_url="https://youli.example/",
        mdm_base_url="https://mdm.example",
    )
    services = replace(services, config=replace(services.config, youli=config))
    service = YouliAuthService(services=services, users=UserManager(services), cfg=config)
    _mock_client(monkeypatch)
    _mock_password(monkeypatch)
    verify = service._verify_identity

    async def mutate(issued):
        await verify(issued)
        if mutation == "unlink":
            await service.unlink_employee(uid)
        elif mutation == "identity":
            services.user_repo.remove_sso_identity(uid, provider_id=provider.id)
        else:
            with services.db.transaction() as conn:
                field, value = {
                    "disabled": ("enabled", 0),
                    "client": ("client_id", "other"),
                    "secret": ("client_secret_enc", b"other"),
                    "region": ("extra", '{"region":"lark"}'),
                }[mutation]
                conn.execute(f"UPDATE sso_providers SET {field}=? WHERE id=?", (value, provider.id))

    monkeypatch.setattr(service, "_verify_identity", mutate)
    with pytest.raises(OctopError):
        await service.bind_employee(
            user_id=uid,
            mobile="123456",
            password="correct",
            feishu_claim=claim,
        )
    assert not service.binding_status(uid)["bound"]
    assert services.youli_session_repo.list_active_for_user(uid) == []


@pytest.mark.asyncio
async def test_binding_http_full_path_and_rejections(ctx, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from tests.unit.youli.test_youli_service import _mock_client, _mock_password

    from octop.api.deps import current_user, get_server
    from octop.api.routers.auth_youli import router
    from octop.config import YouliConfig
    from octop.infra.users.identity import User

    services, auth, uid, other, provider, remote = ctx
    config = YouliConfig(
        enabled=True,
        base_url="https://youli.example",
        client_id="octop",
        client_secret="secret",
        audience="octop",
        callback_url="https://octop.example/callback",
        business_entry_url="https://youli.example/",
        mdm_base_url="https://mdm.example",
    )
    services = replace(services, config=replace(services.config, youli=config))
    app = FastAPI()
    app.include_router(router, prefix="/api/auth/youli")
    server = SimpleNamespace(
        services=services, user_manager=UserManager(services), sso_service=auth.adapter._service
    )
    app.dependency_overrides[get_server] = lambda: server
    app.dependency_overrides[current_user] = lambda: User(
        id=uid, username="person", role="user", display_name=None
    )

    @app.exception_handler(OctopError)
    async def error_handler(request, exc):
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    _mock_client(monkeypatch)
    _mock_password(monkeypatch)
    headers = {"Authorization": "Bearer session"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://octop.example"
    ) as client:

        async def get_proof():
            started = await client.post(
                "/api/auth/youli/binding/reauth/start",
                headers=headers,
                json={"provider_id": provider.id},
            )
            assert started.status_code == 200
            state = parse_qs(urlparse(started.json()["authorization_url"]).query)["state"][0]
            response = await client.post(
                "/api/auth/youli/binding/reauth/callback",
                headers=headers,
                json={"state": state, "code": "code"},
            )
            assert response.status_code == 200
            assert (
                await client.post(
                    "/api/auth/youli/binding/reauth/callback",
                    headers=headers,
                    json={"state": state, "code": "code"},
                )
            ).status_code != 200
            return response.json()["proof"]

        async def bind(token, password="correct", request_headers=headers):
            return await client.post(
                "/api/auth/youli/binding",
                headers=request_headers,
                json={"mobile": "123456", "password": password, "proof": token},
            )

        assert (await bind("invalid")).status_code != 200
        token = await get_proof()
        assert (
            await bind(token, request_headers={"Authorization": "Bearer wrong"})
        ).status_code != 200
        app.dependency_overrides[current_user] = lambda: User(
            id=other, username="other", role="user", display_name=None
        )
        assert (await bind(token)).status_code != 200
        app.dependency_overrides[current_user] = lambda: User(
            id=uid, username="person", role="user", display_name=None
        )
        assert (await bind(token)).json()["delegation_active"] is True
        assert (await bind(token)).status_code != 200
        assert (
            services.user_repo.get_by_sso(services.sso_repo.get_by_kind("youli").id, "youli:21:31")
            is None
        )
        token = await get_proof()
        assert (await bind(token)).json()["delegation_active"] is True
        token = await get_proof()
        assert (await bind(token, password="wrong")).status_code != 200
        assert (await bind(token)).status_code != 200
        token = await get_proof()
        with services.db.transaction() as conn:
            conn.execute(
                "UPDATE feishu_erp_reauth_proof SET expires_at=? WHERE proof_hash=?",
                (int(time.time()), digest(token)),
            )
        assert (await bind(token)).status_code != 200
        remote["tenant"] = "wrong"
        started = await client.post(
            "/api/auth/youli/binding/reauth/start",
            headers=headers,
            json={"provider_id": provider.id},
        )
        state = parse_qs(urlparse(started.json()["authorization_url"]).query)["state"][0]
        assert (
            await client.post(
                "/api/auth/youli/binding/reauth/callback",
                headers=headers,
                json={"state": state, "code": "code"},
            )
        ).status_code != 200


@pytest.mark.parametrize(
    "mutation", ["unlink", "identity", "disabled", "client", "secret", "region"]
)
def test_claim_commit_transaction_rejects_changed_identity_or_config(ctx, mutation):
    services, auth, uid, _, provider, _ = ctx
    claim = auth.claim(uid, "session", proof(ctx))
    if mutation == "unlink":
        services.youli_identity_binding_repo.unlink(uid, 0)
    elif mutation == "identity":
        services.user_repo.remove_sso_identity(uid, provider_id=provider.id)
        services.user_repo.upsert_sso_identity(
            uid, provider_id=provider.id, subject=claim["subject"]
        )
    else:
        with services.db.transaction() as conn:
            field, value = {
                "disabled": ("enabled", 0),
                "client": ("client_id", "other"),
                "secret": ("client_secret_enc", b"other"),
                "region": ("extra", '{"region":"lark"}'),
            }[mutation]
            conn.execute(f"UPDATE sso_providers SET {field}=? WHERE id=?", (value, provider.id))
    with pytest.raises(ValueError):
        services.youli_identity_binding_repo.bind(
            user_id=uid,
            provider_id=0,
            subject="youli:21:31",
            feishu_claim=claim,
            feishu_subject=claim["subject"],
            sid="sid",
            account_user_id=11,
            account_username="person",
            company_id=21,
            employee_id=31,
            industry=1,
            token_blob=b"encrypted",
            token_expires_at=int(time.time()) + 3600,
            abs_expires_at=int(time.time()) + 3600,
        )
    assert services.youli_identity_binding_repo.get(uid) is None
    assert services.youli_session_repo.list_active_for_user(uid) == []
