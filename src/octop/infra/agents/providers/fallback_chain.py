"""Admin-configured backup models for turns pinned to an explicit model.

AUTO routing already picks the next healthy candidate. A turn that names one
model has no candidates, so it fails on the first unhealthy model. The chain
lists ``provider/model`` refs to try, in order, after that model fails with a
failover-safe error.
"""

from __future__ import annotations

import json
from typing import Any

from octop.infra.errors import ErrorCode, OctopError

SETTINGS_KEY = "model_fallback_chain"
MAX_CHAIN = 5


def load_fallback_chain(settings_repo: Any) -> list[str]:
    raw = settings_repo.get(SETTINGS_KEY)
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def save_fallback_chain(settings_repo: Any, refs: list[str]) -> list[str]:
    cleaned: list[str] = []
    for raw in refs:
        ref = str(raw).strip()
        if not ref or ref in cleaned:
            continue
        if "/" not in ref:
            raise OctopError(
                ErrorCode.FORBIDDEN, "fallback model must be provider/model", status=400
            )
        cleaned.append(ref)
    if len(cleaned) > MAX_CHAIN:
        raise OctopError(ErrorCode.FORBIDDEN, "too many fallback models", status=400)
    settings_repo.set(SETTINGS_KEY, json.dumps(cleaned))
    return cleaned


__all__ = ["MAX_CHAIN", "SETTINGS_KEY", "load_fallback_chain", "save_fallback_chain"]
