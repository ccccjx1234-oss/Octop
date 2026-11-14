"""Collectable runtime diagnostics: request latency/errors, event-loop lag, RSS heartbeat."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_SLOW_HTTP_MS = 500.0
_LOOP_LAG_WARN_MS = 200.0
_HEARTBEAT_INTERVAL_S = 60.0
_DEFAULT_IDLE_EVICT_S = 1800.0


def _read_rss_mb() -> float:
    """Best-effort current process resident memory in MiB without extra dependencies."""
    if sys.platform == "linux":
        try:
            with open("/proc/self/statm", encoding="utf-8") as fh:
                parts = fh.read().split()
            if len(parts) >= 2:
                pages = int(parts[1])
                page_size = os.sysconf("SC_PAGE_SIZE")
                return round((pages * page_size) / (1024.0 * 1024.0), 1)
        except (OSError, ValueError):
            pass
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        scale = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
        return round(float(usage.ru_maxrss) / scale, 1)
    except Exception:  # noqa: BLE001
        return 0.0


def parse_idle_evict_seconds() -> float:
    raw = os.environ.get("OCTOP_USER_RUNTIME_IDLE_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_IDLE_EVICT_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_IDLE_EVICT_S


@dataclass(frozen=True)
class TelemetryEvent:
    ts: int
    kind: str
    detail: dict[str, Any]


class RuntimeTelemetry:
    """Thread-safe in-memory rolling ring of latency/error/lag telemetry."""

    def __init__(self, *, maxlen: int = 200) -> None:
        self._lock = threading.Lock()
        self._events: deque[TelemetryEvent] = deque(maxlen=maxlen)
        self.started_at = time.time()
        self.total_requests = 0
        self.error_requests = 0
        self.slow_requests = 0
        self.loop_lag_events = 0
        self.max_loop_lag_ms = 0.0
        self.last_heartbeat_ts = 0

    def record_request(
        self,
        *,
        req_id: str,
        method: str,
        path: str,
        status: int,
        duration_ms: float,
        user_id: int | None = None,
    ) -> None:
        is_error = status >= 400
        is_slow = duration_ms >= _SLOW_HTTP_MS
        with self._lock:
            self.total_requests += 1
            if is_error:
                self.error_requests += 1
            if is_slow:
                self.slow_requests += 1
            if is_error or is_slow:
                self._events.append(
                    TelemetryEvent(
                        ts=int(time.time()),
                        kind="http_error" if is_error else "http_slow",
                        detail={
                            "req_id": req_id,
                            "method": method,
                            "path": path,
                            "status": status,
                            "duration_ms": round(duration_ms, 2),
                            **({"user_id": user_id} if user_id is not None else {}),
                        },
                    )
                )

    def record_loop_lag(self, lag_ms: float) -> None:
        with self._lock:
            self.loop_lag_events += 1
            if lag_ms > self.max_loop_lag_ms:
                self.max_loop_lag_ms = round(lag_ms, 2)
            self._events.append(
                TelemetryEvent(
                    ts=int(time.time()),
                    kind="loop_lag",
                    detail={"lag_ms": round(lag_ms, 2)},
                )
            )

    def snapshot(self, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            events = [
                {"ts": e.ts, "kind": e.kind, **e.detail}
                for e in list(self._events)[-50:]
            ]
            data: dict[str, Any] = {
                "uptime_s": int(time.time() - self.started_at),
                "rss_mb": _read_rss_mb(),
                "total_requests": self.total_requests,
                "error_requests": self.error_requests,
                "slow_requests": self.slow_requests,
                "loop_lag_events": self.loop_lag_events,
                "max_loop_lag_ms": self.max_loop_lag_ms,
                "last_heartbeat_ts": self.last_heartbeat_ts,
                "recent_events": events,
            }
        if extra:
            data.update(extra)
        return data

    async def run_monitor(
        self,
        stop_event: asyncio.Event,
        *,
        stats_provider: Callable[[], dict[str, Any]] | None = None,
        idle_evictor: Callable[[float], Awaitable[int]] | None = None,
    ) -> None:
        """Sample event-loop lag (1s tick) and emit 60s runtime heartbeats."""
        last_hb = time.monotonic()
        while not stop_event.is_set():
            expected = time.monotonic() + 1.0
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1.0)
                break
            except TimeoutError:
                pass
            now = time.monotonic()
            lag_ms = max(0.0, (now - expected) * 1000.0)
            if lag_ms >= _LOOP_LAG_WARN_MS:
                self.record_loop_lag(lag_ms)
                logger.warning("event_loop_lag lag_ms=%.1f", lag_ms)

            if now - last_hb >= _HEARTBEAT_INTERVAL_S:
                last_hb = now
                extra = stats_provider() if stats_provider is not None else {}
                if idle_evictor is not None:
                    idle_s = parse_idle_evict_seconds()
                    if idle_s > 0:
                        try:
                            evicted = await idle_evictor(idle_s)
                            if evicted:
                                extra["evicted_runtimes"] = evicted
                        except Exception:  # noqa: BLE001
                            logger.exception("idle user runtime eviction failed")
                snap = self.snapshot(extra=extra)
                with self._lock:
                    self.last_heartbeat_ts = int(time.time())
                logger.info(
                    "runtime_heartbeat rss_mb=%s total_req=%s slow_req=%s err_req=%s "
                    "loop_lags=%s max_lag_ms=%s agents_running=%s user_runtimes=%s",
                    snap.get("rss_mb"),
                    snap.get("total_requests"),
                    snap.get("slow_requests"),
                    snap.get("error_requests"),
                    snap.get("loop_lag_events"),
                    snap.get("max_loop_lag_ms"),
                    snap.get("agents_running", 0),
                    snap.get("user_runtimes", 0),
                )
