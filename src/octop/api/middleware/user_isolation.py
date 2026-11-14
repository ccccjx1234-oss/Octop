"""Keep authenticated actor scope alive through HTTP streams and WebSockets."""

from __future__ import annotations

import re
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

from octop.api.deps import (
    async_authenticate_request,
    async_resolve_user_from_token,
    authenticate_request,
    is_jwt_exempt_request,
    resolve_user_from_token,
)
from octop.infra.auth.youli.context import current_youli_sid, set_current_youli_sid
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.isolation.context import actor_scope, enabled

_ORIG_AUTHENTICATE = authenticate_request
_ORIG_RESOLVE_TOKEN = resolve_user_from_token
_SKIP_RUNTIME_SUFFIXES = frozenset({"start", "stop", "restart", "avatar", "read", "status"})

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
                cached_user = getattr(request.state, "octop_user", None)
                if cached_user is not None:
                    user = cached_user
                    cached_sid = getattr(request.state, "octop_youli_sid", None)
                    if cached_sid:
                        set_current_youli_sid(str(cached_sid))
                elif authenticate_request is not _ORIG_AUTHENTICATE:
                    user = authenticate_request(request, self.server)
                    request.state.octop_user = user
                    sid = current_youli_sid()
                    if sid:
                        request.state.octop_youli_sid = sid
                else:
                    user = await async_authenticate_request(request, self.server)
            else:
                ws = WebSocket(scope, receive, send)
                token = ws.query_params.get("token", "")
                if not token:
                    raise OctopError(ErrorCode.AUTH_FAILED, "missing token")
                if resolve_user_from_token is not _ORIG_RESOLVE_TOKEN:
                    user = resolve_user_from_token(self.server, token)
                else:
                    user = await async_resolve_user_from_token(self.server, token)
            if any(path == prefix or path.startswith(prefix + "/") for prefix in _HOST_SURFACES):
                raise OctopError(ErrorCode.FORBIDDEN, "host control has no user-isolated adapter")
            with actor_scope(user.id):
                match = _AGENT.match(path)
                runtime = getattr(self.server, "app_runtime", None)
                if (
                    match
                    and runtime is not None
                    and path.rsplit("/", 1)[-1] not in _SKIP_RUNTIME_SUFFIXES
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
