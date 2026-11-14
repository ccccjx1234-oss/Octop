"""Lightweight ASGI request telemetry: X-Request-ID, slow/error structured logs."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from octop.infra.isolation.context import actor_id

logger = logging.getLogger(__name__)
_SLOW_MS = 500.0
_QUIET_HEALTH_PATHS = frozenset({"/api/health", "/api/health/isolation"})


def _extract_request_id(scope: Scope) -> str:
    for name, value in scope.get("headers") or ():
        if name.lower() == b"x-request-id":
            text = value.decode("latin-1", errors="ignore").strip()
            if text:
                return text[:64]
    return uuid.uuid4().hex[:12]


class RequestTelemetryMiddleware:
    """Attach X-Request-ID and record structured request latency/error metrics."""

    def __init__(self, app: ASGIApp, *, server: Any) -> None:
        self.app = app
        self.server = server

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "")
        if not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method") or "GET")
        req_id = _extract_request_id(scope)
        started = time.monotonic()
        status_code = 500

        async def _send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message.get("status") or 200)
                headers = list(message.get("headers") or [])
                if not any(k.lower() == b"x-request-id" for k, _ in headers):
                    headers.append((b"x-request-id", req_id.encode("ascii")))
                    message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            duration_ms = (time.monotonic() - started) * 1000.0
            telemetry = getattr(self.server, "telemetry", None)
            uid = actor_id()
            if uid is None:
                state = scope.get("state")
                if isinstance(state, dict):
                    raw_id = getattr(state.get("octop_user"), "id", None)
                    if isinstance(raw_id, int):
                        uid = raw_id
            if telemetry is not None:
                telemetry.record_request(
                    req_id=req_id,
                    method=method,
                    path=path,
                    status=status_code,
                    duration_ms=duration_ms,
                    user_id=uid,
                )
            if status_code >= 500:
                logger.error(
                    "http_request req_id=%s method=%s path=%s status=%s duration_ms=%.2f uid=%s",
                    req_id,
                    method,
                    path,
                    status_code,
                    duration_ms,
                    uid,
                )
            elif (
                status_code >= 400 or duration_ms >= _SLOW_MS
            ) and path not in _QUIET_HEALTH_PATHS:
                logger.warning(
                    "http_request req_id=%s method=%s path=%s status=%s duration_ms=%.2f uid=%s",
                    req_id,
                    method,
                    path,
                    status_code,
                    duration_ms,
                    uid,
                )
