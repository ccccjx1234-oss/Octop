"""Shared HTTP connection pooling and diagnostic logging for Youli and MCN clients."""

from __future__ import annotations

import atexit
import logging
import ssl
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_ORIGINAL_CLIENT = httpx.Client
_POOL_LOCK = threading.Lock()
_MAX_POOLED_CLIENTS = 24
_SLOW_REQUEST_MS = 500.0


@dataclass
class _PooledEntry:
    client: httpx.Client
    verify_ref: ssl.SSLContext | bool


_CLIENT_POOL: dict[
    tuple[str, int | bool, tuple[float | None, float | None, float | None, float | None]],
    _PooledEntry,
] = {}


def _normalize_timeout(
    timeout: httpx.Timeout | float | int | None,
) -> tuple[tuple[float | None, float | None, float | None, float | None], httpx.Timeout]:
    if isinstance(timeout, httpx.Timeout):
        return (timeout.connect, timeout.read, timeout.write, timeout.pool), timeout
    val = float(timeout if timeout is not None else 10.0)
    resolved = httpx.Timeout(val, connect=min(val, 5.0))
    return (resolved.connect, resolved.read, resolved.write, resolved.pool), resolved


def _get_pooled_client(
    *,
    url: str,
    verify: ssl.SSLContext | bool,
    timeout: httpx.Timeout | float | int | None,
) -> httpx.Client:
    netloc = urlparse(url).netloc or url
    verify_key: int | bool = id(verify) if isinstance(verify, ssl.SSLContext) else bool(verify)
    timeout_key, timeout_obj = _normalize_timeout(timeout)
    key = (netloc, verify_key, timeout_key)
    with _POOL_LOCK:
        entry = _CLIENT_POOL.get(key)
        if entry is not None and entry.verify_ref == verify and not entry.client.is_closed:
            return entry.client
        if len(_CLIENT_POOL) >= _MAX_POOLED_CLIENTS:
            oldest_key = next(iter(_CLIENT_POOL))
            old = _CLIENT_POOL.pop(oldest_key)
            with suppress(Exception):
                old.client.close()
        client = _ORIGINAL_CLIENT(
            timeout=timeout_obj,
            limits=httpx.Limits(
                max_connections=32,
                max_keepalive_connections=16,
                keepalive_expiry=60.0,
            ),
            verify=verify,
        )
        _CLIENT_POOL[key] = _PooledEntry(client=client, verify_ref=verify)
        return client


def close_pooled_clients() -> None:
    """Close all pooled httpx clients (used on shutdown or test cleanup)."""
    with _POOL_LOCK:
        entries = list(_CLIENT_POOL.values())
        _CLIENT_POOL.clear()
    for entry in entries:
        with suppress(Exception):
            entry.client.close()


atexit.register(close_pooled_clients)


def perform_request(
    client_cls: type[httpx.Client],
    method: str,
    url: str,
    *,
    timeout: httpx.Timeout | float | int | None,
    verify: ssl.SSLContext | bool,
    json: Any = None,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
    target_label: str = "",
) -> Any:
    """Execute an HTTP request reusing a pooled client unless monkeypatched."""
    label = target_label or urlparse(url).path or url
    started = time.monotonic()
    try:
        if client_cls is _ORIGINAL_CLIENT:
            client = _get_pooled_client(url=url, verify=verify, timeout=timeout)
            kwargs: dict[str, Any] = {}
            if json is not None:
                kwargs["json"] = json
            if params is not None:
                kwargs["params"] = params
            if headers is not None:
                kwargs["headers"] = headers
            if content is not None:
                kwargs["content"] = content
            response = client.request(method, url, **kwargs)
        else:
            with client_cls(timeout=timeout, verify=verify) as test_client:
                if method.upper() == "POST" and not hasattr(test_client, "request"):
                    post_kwargs: dict[str, Any] = {}
                    if json is not None:
                        post_kwargs["json"] = json
                    if headers is not None:
                        post_kwargs["headers"] = headers
                    if content is not None:
                        post_kwargs["content"] = content
                    response = test_client.post(url, **post_kwargs)
                else:
                    response = test_client.request(
                        method,
                        url,
                        json=json,
                        params=params,
                        headers=headers,
                    )
    except httpx.HTTPError as exc:
        duration_ms = (time.monotonic() - started) * 1000.0
        logger.warning(
            "upstream_http transport_error method=%s target=%s duration_ms=%.2f exc_type=%s detail=%s",
            method,
            label,
            duration_ms,
            type(exc).__name__,
            exc,
        )
        raise
    duration_ms = (time.monotonic() - started) * 1000.0
    status = getattr(response, "status_code", None)
    if (status is not None and int(status) >= 400) or duration_ms >= _SLOW_REQUEST_MS:
        logger.info(
            "upstream_http completed method=%s target=%s status=%s duration_ms=%.2f",
            method,
            label,
            status,
            duration_ms,
        )
    return response
