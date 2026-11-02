"""Public shapes for secrets that list and read endpoints must not echo."""

from __future__ import annotations

from typing import Any

# Local runtime rows store these sentinels in ``api_key``. They identify the
# runtime (Ollama, ONNX, LM Studio) and are not cloud credentials, so the
# dashboard can still tell those rows apart from a masked key.
_LOCAL_PLACEHOLDERS = frozenset({"ollama", "onnx", "lm-studio", "sk-local"})


def api_key_public_fields(api_key: str | None) -> dict[str, Any]:
    """Return ``api_key_set`` and a non-secret ``api_key_preview``.

    The raw key is never included. Local placeholders pass through so the
    models page can keep classifying those providers. Anything else is masked.
    """
    raw = (api_key or "").strip()
    if not raw:
        return {"api_key_set": False, "api_key_preview": None}
    lowered = raw.lower()
    if lowered in _LOCAL_PLACEHOLDERS:
        return {"api_key_set": True, "api_key_preview": lowered}
    preview = "•" * len(raw) if len(raw) <= 8 else f"{raw[:4]}{'•' * 6}{raw[-4:]}"
    return {"api_key_set": True, "api_key_preview": preview}
