"""Shared formatting, numeric, date, and bounded idempotency helpers for MCN tools."""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from octop.infra.auth.youli.mcn_client import McnClient, McnClientError
from octop.infra.auth.youli.pagination import post_with_page, slim_rows

_MAX_OUTPUT_CHARS = 24000
_CACHE_TTL_SECONDS = 600.0
_CACHE_MAX_ENTRIES = 512


class BoundedTTLCache(OrderedDict[str, tuple[float, str]]):
    """In-process idempotency cache with TTL expiry and bounded LRU capacity."""

    def __init__(
        self,
        *,
        max_entries: int = _CACHE_MAX_ENTRIES,
        ttl_seconds: float = _CACHE_TTL_SECONDS,
    ) -> None:
        super().__init__()
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds

    def prune(self, now: float | None = None) -> None:
        cutoff = (now if now is not None else time.time()) - self._ttl_seconds
        expired = [k for k, (ts, _val) in self.items() if ts <= cutoff]
        for key in expired:
            self.pop(key, None)
        while len(self) > self._max_entries:
            self.popitem(last=False)

    def recall(self, key: str) -> str | None:
        now = time.time()
        entry = self.get(key)
        if entry is None:
            return None
        ts, value = entry
        if now - ts >= self._ttl_seconds:
            self.pop(key, None)
            return None
        self.move_to_end(key)
        return value

    def remember(self, key: str, result: str) -> None:
        now = time.time()
        self[key] = (now, result)
        self.move_to_end(key)
        self.prune(now)


_SAVED_QUOTES = BoundedTTLCache()


def _money(value: Any) -> Decimal:
    return Decimal(str(value if value not in (None, "") else "0")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def _money_number(value: Decimal) -> float:
    return float(value)


def _line_quantity(spec: dict[str, Any]) -> int:
    raw = spec.get("quantity")
    if raw in (None, ""):
        raw = spec.get("count") if spec.get("count") not in (None, "") else 1
    quantity = int(str(raw))
    if quantity < 1 or quantity > 9999:
        raise ValueError("quantity")
    return quantity


def _cache_scope(client: McnClient) -> str:
    fingerprint = getattr(client, "token_fingerprint", None) or getattr(client, "_token", None)
    return str(fingerprint) if fingerprint else "anonymous"


def _scoped_cache_key(client: McnClient, key: str) -> str:
    return f"{_cache_scope(client)}:{key}"


def _recall(client: McnClient, key: str) -> str | None:
    return _SAVED_QUOTES.recall(_scoped_cache_key(client, key))


def _remember(client: McnClient, key: str, result: str) -> None:
    _SAVED_QUOTES.remember(_scoped_cache_key(client, key), result)


def payload_digest(payload: dict[str, Any]) -> str:
    """Stable SHA-256 hex digest for JSON-serializable request payloads."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def idempotent_post(
    client: McnClient,
    cache_key: str,
    path: str,
    payload: dict[str, Any],
) -> str:
    """POST *payload* once per employee cache window and remember its serialized result."""
    cached = _recall(client, cache_key)
    if cached is not None:
        return f"重复提交已忽略，上次结果：{cached}"
    result = _json(client.post(path, payload))
    _remember(client, cache_key, result)
    return result


def _json(data: Any, *, max_chars: int = _MAX_OUTPUT_CHARS) -> str:
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...(结果已截断)"


def _slim(data: Any, keys: tuple[str, ...]) -> Any:
    if not isinstance(data, list):
        return data
    return [
        {key: item.get(key) for key in keys if key in item}
        for item in data
        if isinstance(item, dict)
    ]


def _post_page(
    client: McnClient,
    path: str,
    payload: dict[str, Any],
    keys: tuple[str, ...],
    *,
    page_num: int,
    page_size: int,
) -> dict[str, Any]:
    rows, page_info = post_with_page(
        client,
        path,
        payload,
        page_num=page_num,
        page_size=page_size,
    )
    return {"items": slim_rows(rows, keys), "page": page_info}


def _normalize_date(value: Any, field_name: str, *, required: bool = False) -> str:
    raw = str(value or "").strip()
    if not raw:
        if required:
            raise McnClientError(f"{field_name} 不能为空")
        return ""
    candidate = raw[:10]
    try:
        date.fromisoformat(candidate)
    except ValueError as exc:
        raise McnClientError(f"{field_name} 日期格式不正确，需为 yyyy-MM-dd") from exc
    return candidate


def _parse_json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            return {}
    return {}
