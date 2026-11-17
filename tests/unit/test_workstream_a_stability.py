"""Focused unit tests for Workstream A stability, pooling, non-blocking auth, and telemetry."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import certifi
import httpx
import pytest
from fastapi import FastAPI

from octop.api import deps
from octop.config import OctopConfig, YouliConfig
from octop.infra.auth.youli import http_pool
from octop.infra.auth.youli.client import YouliClient, YouliClientUnavailable
from octop.infra.auth.youli.service import YouliAuthService
from octop.infra.auth.youli.tls import build_verify
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.errors import OctopError
from octop.infra.telemetry import RuntimeTelemetry
from octop.infra.users.identity import User
from octop.infra.users.manager import UserManager
from octop.infra.utils.paths import PathLayout


def test_build_verify_caches_unchanged_ca_file() -> None:
    path = certifi.where()
    first = build_verify(path)
    second = build_verify(path)
    assert first is second
    assert build_verify("") is True


def test_http_pool_reuses_client_and_closes_cleanly() -> None:
    http_pool.close_pooled_clients()
    c1 = http_pool._get_pooled_client(
        url="https://mcn.example/api/a",
        verify=True,
        timeout=10.0,
    )
    c2 = http_pool._get_pooled_client(
        url="https://mcn.example/api/b",
        verify=True,
        timeout=10.0,
    )
    assert c1 is c2
    http_pool.close_pooled_clients()
    assert c1.is_closed


@pytest.mark.asyncio
async def test_youli_start_maps_unavailable_to_503_and_staff_login_consumes_on_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = PathLayout(tmp_path / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    cfg = OctopConfig(
        youli=YouliConfig(
            enabled=True,
            base_url="https://youli.example",
            client_id="octop",
            client_secret="secret",
            audience="octop",
            callback_url="https://octop.example/login/callback",
            business_entry_url="https://youli.example/octop-login",
            mdm_base_url="https://mdm.example",
        )
    )
    services = build_shared_services(db=db, paths=paths, config=cfg)
    svc = YouliAuthService(services=services, users=UserManager(services), cfg=cfg.youli)

    monkeypatch.setattr(
        YouliClient,
        "register",
        lambda self, **kwargs: (_ for _ in ()).throw(YouliClientUnavailable("down")),
    )
    with pytest.raises(OctopError) as exc_info:
        await svc.start(public_base="https://octop.example")
    assert exc_info.value.status == 503

    created_ids: list[str] = []
    monkeypatch.setattr(
        YouliClient,
        "register",
        lambda self, **kwargs: created_ids.append(kwargs["request_id"]),
    )
    monkeypatch.setattr(
        YouliClient,
        "password_login",
        lambda self, **kwargs: (_ for _ in ()).throw(YouliClientUnavailable("auth down")),
    )
    with pytest.raises(OctopError) as staff_exc:
        await svc.staff_login(
            mobile="13800000000",
            password="bad",
            public_base="https://octop.example",
        )
    assert staff_exc.value.status == 503
    assert len(created_ids) == 1
    row = services.youli_login_request_repo.get(created_ids[0])
    assert row is not None and row.consumed_at is not None


@pytest.mark.asyncio
async def test_async_revalidation_coalesces_concurrent_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def slow_verify(*, services, cfg, session):
        nonlocal calls
        calls += 1
        time.sleep(0.05)

    monkeypatch.setattr(
        "octop.infra.auth.youli.service.revalidate_business_session",
        slow_verify,
    )
    deps._YOULI_REVALIDATED_AT.clear()
    deps._INFLIGHT_REVALIDATIONS.clear()
    server = SimpleNamespace(
        services=SimpleNamespace(
            config=SimpleNamespace(
                youli=YouliConfig(
                    enabled=True,
                    mdm_base_url="https://mdm.example",
                    revalidate_seconds=30,
                )
            )
        )
    )
    session = SimpleNamespace(sid="sid-coalesce-1")
    await asyncio.gather(
        deps._async_revalidate_youli_session(server, session),
        deps._async_revalidate_youli_session(server, session),
        deps._async_revalidate_youli_session(server, session),
    )
    assert calls == 1


def test_runtime_telemetry_records_events_and_snapshot() -> None:
    t = RuntimeTelemetry()
    t.record_request(req_id="abc", method="GET", path="/api/x", status=200, duration_ms=10.0)
    t.record_request(
        req_id="def", method="POST", path="/api/y", status=500, duration_ms=650.0, user_id=2
    )
    t.record_loop_lag(250.5)
    snap = t.snapshot(extra={"agents_running": 3})
    assert snap["total_requests"] == 2
    assert snap["error_requests"] == 1
    assert snap["slow_requests"] == 1
    assert snap["loop_lag_events"] == 1
    assert snap["max_loop_lag_ms"] == 250.5
    assert snap["agents_running"] == 3
    assert len(snap["recent_events"]) == 2


def test_http_pool_accepts_httpx_timeout_and_reuses_client() -> None:
    from octop.infra.auth.youli.http_pool import _get_pooled_client, close_pooled_clients

    close_pooled_clients()
    try:
        t_obj = httpx.Timeout(10.0, connect=5.0)
        c1 = _get_pooled_client(url="https://mcn.example/api/a", verify=True, timeout=t_obj)
        c2 = _get_pooled_client(url="https://mcn.example/api/b", verify=True, timeout=t_obj)
        c3 = _get_pooled_client(url="https://mcn.example/api/c", verify=True, timeout=10.0)
        assert c1 is c2
        assert c1 is c3
    finally:
        close_pooled_clients()


@pytest.mark.asyncio
async def test_request_telemetry_captures_user_id_from_jwt_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from starlette.responses import JSONResponse

    from octop.api.middleware.jwt_auth import install as install_jwt_auth
    from octop.api.middleware.request_telemetry import RequestTelemetryMiddleware

    telemetry = RuntimeTelemetry()
    app = FastAPI()

    @app.get("/api/ping")
    async def ping() -> JSONResponse:
        return JSONResponse({"ok": False}, status_code=400)

    server = SimpleNamespace(telemetry=telemetry, app_runtime=None)
    app.state.server = server
    monkeypatch.setattr(
        "octop.api.deps.async_resolve_user_from_token",
        AsyncMock(return_value=User(7, "alice", "user", None)),
    )
    monkeypatch.setattr(
        "octop.api.middleware.jwt_auth.maybe_sliding_renew_token",
        lambda *_a, **_kw: None,
    )
    install_jwt_auth(app, server)
    app.add_middleware(RequestTelemetryMiddleware, server=server)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/api/ping", headers={"Authorization": "Bearer fake-token"})

    assert resp.status_code == 400
    snap = telemetry.snapshot()
    assert snap["recent_events"][0]["user_id"] == 7
