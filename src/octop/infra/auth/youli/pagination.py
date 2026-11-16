"""Shared pagination helpers for MCN list endpoints and internal lookups."""

from __future__ import annotations

from typing import Any


def parse_page_args(
    args: dict[str, Any],
    *,
    default_size: int = 20,
    max_size: int = 100,
) -> tuple[int, int]:
    """Normalize ``page`` and ``page_size`` arguments to valid 1-based bounds."""
    try:
        page_num = max(1, int(args.get("page") or 1))
    except (TypeError, ValueError):
        page_num = 1
    try:
        raw_size = args.get("page_size")
        page_size = int(raw_size) if raw_size is not None and raw_size != "" else default_size
    except (TypeError, ValueError):
        page_size = default_size
    page_size = min(max_size, max(1, page_size))
    return page_num, page_size


def _safe_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_rows(data: Any) -> list[dict[str, Any]]:
    """Extract row dicts from either a raw list or a CommonResult envelope."""
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("data", "list", "records", "rows"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def extract_page_data(
    response: Any,
    *,
    page_num: int,
    page_size: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return ``(rows, page_meta)`` from a CommonResult envelope or plain list."""
    rows = extract_rows(response)
    raw_page = response.get("page") if isinstance(response, dict) else None
    meta = raw_page if isinstance(raw_page, dict) else {}

    actual_num = _safe_int(meta.get("pageNum")) or page_num
    total_count = _safe_int(meta.get("totalCount"))
    total_page = _safe_int(meta.get("totalPage"))
    if total_page is None and total_count is not None and page_size > 0:
        total_page = (total_count + page_size - 1) // page_size if total_count > 0 else 0

    if total_page is not None:
        has_more = actual_num < total_page
    elif total_count is not None:
        has_more = actual_num * page_size < total_count
    else:
        has_more = len(rows) >= page_size

    page_info: dict[str, Any] = {
        "pageNum": actual_num,
        "pageSize": page_size,
        "totalCount": total_count,
        "totalPage": total_page,
        "hasMore": has_more,
    }
    return rows, page_info


def slim_rows(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    """Keep only the selected fields from each row dictionary."""
    return [{key: row.get(key) for key in keys if key in row} for row in rows]


def post_with_page(
    client: Any,
    path: str,
    payload: dict[str, Any],
    *,
    page_num: int,
    page_size: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """POST via ``post_with_meta`` when available and normalize rows + page metadata."""
    post_meta = getattr(client, "post_with_meta", None)
    raw = post_meta(path, payload) if callable(post_meta) else client.post(path, payload)
    return extract_page_data(raw, page_num=page_num, page_size=page_size)


def post_all_pages(
    client: Any,
    path: str,
    base_payload: dict[str, Any],
    *,
    page_size: int = 100,
    max_pages: int = 20,
    strict_list: bool = False,
) -> list[dict[str, Any]]:
    """Collect rows across pages until ``hasMore`` is false or ``max_pages`` is reached."""
    collected: list[dict[str, Any]] = []
    for page_num in range(1, max_pages + 1):
        payload = {**base_payload, "page": {"pageNum": page_num, "pageSize": page_size}}
        post_meta = getattr(client, "post_with_meta", None)
        raw = post_meta(path, payload) if callable(post_meta) else client.post(path, payload)
        if strict_list:
            inner = raw.get("data") if isinstance(raw, dict) and "data" in raw else raw
            if not isinstance(inner, list):
                raise ValueError("invalid list payload")
        batch, page_info = extract_page_data(raw, page_num=page_num, page_size=page_size)
        collected.extend(batch)
        if not batch or not page_info.get("hasMore"):
            return collected
    if strict_list:
        raise OverflowError("pagination limit reached")
    return collected
