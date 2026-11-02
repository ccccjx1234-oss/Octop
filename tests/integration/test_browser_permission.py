"""Browser workbench routes require the browser permission."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.support.auth import create_user
from tests.support.http import ASGIWebSocketSession, ws_token


async def test_browser_workbench_requires_permission(env: Any) -> None:
    client, _srv, admin_auth = env
    user_auth = await create_user(client, admin_auth, username="no-browser")

    sessions = await client.get("/api/browser/harness-sessions", headers=user_auth)
    assert sessions.status_code == 403

    handoff = await client.post(
        "/api/browser/sessions/missing/handoff",
        headers=user_auth,
        json={"target": "user"},
    )
    assert handoff.status_code == 403

    recording = await client.get("/api/browser/record-replay/status", headers=user_auth)
    assert recording.status_code == 403

    admin_sessions = await client.get("/api/browser/harness-sessions", headers=admin_auth)
    assert admin_sessions.status_code != 403

    app = client._octop_app  # type: ignore[attr-defined]
    socket = ASGIWebSocketSession(app, f"/api/browser-stream/ws?token={ws_token(user_auth)}")
    try:
        with pytest.raises(WebSocketDisconnect) as caught:
            await socket.connect()
        assert caught.value.code == 4003
    finally:
        await socket.close()
