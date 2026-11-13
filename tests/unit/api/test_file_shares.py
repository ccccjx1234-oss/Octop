"""Exercise the sharing HTTP contract without starting a server or an LLM."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from starlette.responses import JSONResponse, StreamingResponse

from octop.api.deps import current_user, get_server
from octop.api.middleware.user_isolation import UserIsolationMiddleware
from octop.api.routers.file_shares import router
from octop.infra.isolation.context import actor_id, enabled
from octop.infra.users.identity import User
from octop.infra.utils.paths import PathLayout


@pytest.fixture(autouse=True)
def _reset_isolation_mode():
    enabled.cache_clear()
    yield
    enabled.cache_clear()


@pytest.mark.asyncio
async def test_http_share_requires_named_recipient_and_owner_revocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OCTOP_USER_ISOLATION", "1")
    values: dict[str, str] = {}
    settings = SimpleNamespace(
        get=values.get, set=lambda key, value: values.__setitem__(key, value)
    )
    users = {uid: User(uid, f"user{uid}", "user", None) for uid in (1, 2, 3)}
    services = SimpleNamespace(
        settings_repo=settings,
        paths=PathLayout(tmp_path),
        user_repo=SimpleNamespace(
            get=users.get,
            get_by_username=lambda name: (
                SimpleNamespace(id=2, disabled=False) if name == "user2" else None
            ),
        ),
    )
    server = SimpleNamespace(services=services, app_runtime=None)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    from octop.infra.errors import OctopError

    @app.exception_handler(OctopError)
    async def error_handler(_request: object, error: OctopError) -> JSONResponse:
        return JSONResponse(error.to_envelope(), status_code=error.status)

    app.dependency_overrides[get_server] = lambda: server
    selected = [users[1]]
    app.dependency_overrides[current_user] = lambda: selected[0]
    monkeypatch.setattr(
        "octop.api.routers.file_shares.require_agent_workspace",
        AsyncMock(return_value=SimpleNamespace(adownload_bytes=AsyncMock(return_value=b"private"))),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/api/agents/COMMON/file-shares",
            json={"path": "report.txt", "recipient_usernames": ["user2"]},
        )
        assert created.status_code == 200, created.text
        share_id = created.json()["share_id"]
        selected[0] = users[2]
        assert (await client.get(f"/api/file-shares/{share_id}/download")).content == b"private"
        assert (await client.get(f"/api/file-shares/{share_id}")).json()["can_revoke"] is False
        assert (await client.delete(f"/api/file-shares/{share_id}")).status_code == 404
        selected[0] = users[3]
        assert (await client.get(f"/api/file-shares/{share_id}/download")).status_code == 404
        selected[0] = users[1]
        assert (await client.delete(f"/api/file-shares/{share_id}")).status_code == 204
        selected[0] = users[2]
        assert (await client.get(f"/api/file-shares/{share_id}/download")).status_code == 404


@pytest.mark.asyncio
async def test_asgi_scope_remains_private_through_streaming_and_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OCTOP_USER_ISOLATION", "1")
    app = FastAPI()

    @app.get("/api/stream")
    async def stream() -> StreamingResponse:
        async def content():
            before = actor_id()
            await asyncio.sleep(0)
            after = await asyncio.to_thread(actor_id)
            yield f"{before}:{after}"

        return StreamingResponse(content())

    monkeypatch.setattr(
        "octop.api.middleware.user_isolation.authenticate_request",
        lambda request, _: User(int(request.headers["x-test-user"]), "user", "user", None),
    )
    app.add_middleware(UserIsolationMiddleware, server=SimpleNamespace(app_runtime=None))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = await asyncio.gather(
            *[client.get("/api/stream", headers={"x-test-user": str(uid)}) for uid in (1, 2)]
        )
        assert [response.text for response in responses] == ["1:1", "2:2"]
        assert actor_id() is None
        denied = await client.get("/api/filesystem/dirs", headers={"x-test-user": "1"})
        assert denied.status_code == 403


@pytest.mark.asyncio
async def test_host_browser_websocket_cannot_bypass_isolation_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OCTOP_USER_ISOLATION", "1")
    monkeypatch.setattr(
        "octop.api.middleware.user_isolation.resolve_user_from_token",
        lambda *_: User(1, "user", "user", None),
    )
    application = AsyncMock()
    middleware = UserIsolationMiddleware(application, SimpleNamespace(app_runtime=None))
    send = AsyncMock()
    await middleware(
        {
            "type": "websocket",
            "path": "/api/browser-stream/ws",
            "query_string": b"token=test",
            "headers": [],
        },
        AsyncMock(),
        send,
    )
    application.assert_not_called()
    assert send.call_args.args[0] == {"type": "websocket.close", "code": 4003}
