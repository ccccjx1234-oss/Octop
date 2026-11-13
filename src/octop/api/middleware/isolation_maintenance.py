"""Keep diagnostics reachable without exposing execution in maintenance mode."""

from __future__ import annotations

from typing import Any

from starlette.responses import HTMLResponse, JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from octop.infra.errors import ErrorCode, OctopError

_LOGIN_ROUTES = {
    ("GET", "/api/auth/captcha"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/auth/me"),
}


class IsolationMaintenanceMiddleware:
    def __init__(self, app: ASGIApp, server: Any):
        self.app, self.server = app, server

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        policy = getattr(self.server, "isolation_policy", None)
        if policy is None or policy.state != "maintenance" or scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "")
        login_available = self.server.user_manager is not None
        if scope["type"] == "http":
            if method == "GET" and path == "/api/health":
                await self.app(scope, receive, send)
                return
            if login_available and (
                (method == "GET" and path == "/api/health/isolation")
                or (method, path) in _LOGIN_ROUTES
                or (method == "GET" and path.startswith("/api/i18n/"))
            ):
                await self.app(scope, receive, send)
                return
            if path == "/":
                await HTMLResponse(
                    "<!doctype html><html lang='zh'><meta charset='utf-8'>"
                    "<title>Octop 维护模式</title><main>"
                    "<h1>Octop 维护模式 / Maintenance</h1>"
                    "<p>用户隔离尚未就绪，专家执行已暂停。修复环境后重启服务。</p>"
                    "<p>User isolation is unavailable. Agent execution is paused. "
                    "Repair the environment and restart the service.</p>"
                    "<p><a href='/api/health'>健康状态 / Health</a></p>"
                    "<p>管理员诊断：登录后携带 Bearer token 请求 "
                    "<code>GET /api/health/isolation</code>。"
                    "如果登录不可用，请查看服务启动日志。</p></main></html>",
                    status_code=503,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
                return
            if not path.startswith("/api/"):
                await self.app(scope, receive, send)
                return
            error = OctopError(
                ErrorCode.WORKSPACE_OP_UNSUPPORTED,
                "user isolation is unavailable; execution is paused",
                status=503,
                details={"isolation_state": "maintenance"},
            )
            await JSONResponse(
                error.to_envelope(), status_code=503, headers={"Cache-Control": "no-store"}
            )(scope, receive, send)
        else:
            await send({"type": "websocket.close", "code": 1013})
