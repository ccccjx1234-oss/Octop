"""Keep authenticated actor scope alive through HTTP streams and WebSockets."""

from __future__ import annotations

import re
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

from octop.api.deps import authenticate_request, is_jwt_exempt_request, resolve_user_from_token
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.isolation.context import actor_scope, enabled

_AGENT = re.compile(r"^/api/agents/([^/]+)(?:/|$)")
_HOST_SURFACES = (
    "/api/filesystem",
    "/api/browser",
    "/api/browser-stream",
    "/api/desktop",
    "/api/desktop-stream",
    "/api/mobile",
    "/api/mobile-stream",
    "/api/bridges",
    "/api/bridge",
    "/api/downloads",
    "/api/storage-backends",
    "/api/plugins",
)


class UserIsolationMiddleware:
    def __init__(self, app: ASGIApp, server: Any):
        self.app, self.server = app, server

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not enabled() or scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return
        started = False

        async def scoped_send(message: Any) -> None:
            nonlocal started
            if message["type"] in {"http.response.start", "websocket.accept"}:
                started = True
            await send(message)

        try:
            if scope["type"] == "http":
                request = Request(scope)
                if is_jwt_exempt_request(request):
                    await self.app(scope, receive, send)
                    return
                user = authenticate_request(request, self.server)
            else:
                ws = WebSocket(scope, receive, send)
                token = ws.query_params.get("token", "")
                if not token:
                    raise OctopError(ErrorCode.AUTH_FAILED, "missing token")
                user = resolve_user_from_token(self.server, token)
            if any(path == prefix or path.startswith(prefix + "/") for prefix in _HOST_SURFACES):
                raise OctopError(ErrorCode.FORBIDDEN, "host control has no user-isolated adapter")
            with actor_scope(user.id):
                match = _AGENT.match(path)
                runtime = getattr(self.server, "app_runtime", None)
                if (
                    match
                    and runtime is not None
                    and path.rsplit("/", 1)[-1] not in {"start", "stop", "restart"}
                ):
                    await runtime.agent_registry.ensure_user_runtime(match.group(1), user.id)
                await self.app(scope, receive, scoped_send)
        except OctopError as exc:
            if started:
                raise
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4003})
            else:
                await JSONResponse(exc.to_envelope(locale="zh"), status_code=exc.status)(
                    scope, receive, send
                )
