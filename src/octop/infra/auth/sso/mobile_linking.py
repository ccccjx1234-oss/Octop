"""Mobile normalization and tenant-scoped duplicate-account detection for SSO."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

_NON_DIGIT_RE = re.compile(r"[\s\-()]+")
_CN_MAINLAND_RE = re.compile(r"^(?:\+?86|0086)?(1[3-9]\d{9})$")
_E164_DIGITS_RE = re.compile(r"^\+?[1-9]\d{6,14}$")


def normalize_mobile(raw: object) -> str | None:
    """Normalize a mobile number for cross-provider matching.

    Chinese mainland 11-digit numbers (with or without ``+86`` / ``0086``)
    normalize to the bare 11 digits; other international numbers normalize to
    ``+<digits>`` when valid E.164-like. Returns ``None`` for empty/invalid input.
    """
    if not isinstance(raw, str):
        return None
    compact = _NON_DIGIT_RE.sub("", raw.strip())
    if not compact:
        return None
    cn_match = _CN_MAINLAND_RE.fullmatch(compact)
    if cn_match:
        return cn_match.group(1)
    if _E164_DIGITS_RE.fullmatch(compact):
        return compact if compact.startswith("+") else f"+{compact}"
    return None


def parse_allowed_tenants(raw: object) -> list[str]:
    """Parse an allowlist of tenant keys from a list or comma/newline string."""
    items: list[str]
    if isinstance(raw, str):
        items = re.split(r"[\s,;]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        items = [str(item) for item in raw if item is not None]
    else:
        return []
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        cleaned = item.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def extract_feishu_tenant(subject: str) -> str | None:
    """Extract ``tenant_key`` from a scoped ``feishu-v1:...`` subject."""
    if not subject.startswith("feishu-v1:"):
        return None
    try:
        parts = json.loads(subject.removeprefix("feishu-v1:"))
    except ValueError:
        return None
    if isinstance(parts, list) and len(parts) == 4 and isinstance(parts[2], str):
        tenant = parts[2].strip()
        return tenant or None
    return None


def should_auto_link_mobile(
    kind: str,
    extra: Mapping[str, Any] | None,
    subject: str,
) -> bool:
    """Enable duplicate-account detection for the configured providers.

    Feishu requires a non-empty ``allowed_tenants`` list matching the subject's
    ``tenant_key``. A phone match only prompts for authenticated binding.
    """
    if not extra or not bool(extra.get("auto_link_mobile")):
        return False
    if kind == "feishu":
        tenant = extract_feishu_tenant(subject)
        if not tenant:
            return False
        allowed = parse_allowed_tenants(extra.get("allowed_tenants"))
        return tenant in allowed
    return kind == "dingtalk"
