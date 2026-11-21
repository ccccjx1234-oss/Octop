"""Composer defaults carried by a published expert, resolved per installer.

The snapshot records the publisher's knowledge-base ids and connector refs.
Installing never grants access: a viewer only receives knowledge bases they
can already open and connector instances they have already installed. The
seeded workspace manifest drops these ids so a private knowledge base id is
not copied into another user's files.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from octop.infra.agents.experts.catalog import (
    read_workspace_manifest_text,
    workspace_manifest_rel,
)

COMPOSER_DEFAULT_MANIFEST_KEYS = ("knowledge_base_ids", "mcp_servers")


def normalize_id_list(value: Any) -> list[str]:
    """Keep non-empty strings, drop everything else, and preserve order."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text:
            out.append(text)
    return list(dict.fromkeys(out))


def template_composer_defaults(
    manifest: dict[str, Any] | None,
    *,
    source_defaults: tuple[list[str], list[str]] | None = None,
) -> tuple[list[str], list[str]]:
    """Read snapshot defaults, or the live source agent when the snapshot is old.

    A snapshot written by this version always stores both keys, including
    empty lists. Missing keys mean the template was published before composer
    defaults were recorded, so the source agent's current selection is used.
    """
    data = manifest if isinstance(manifest, dict) else {}
    has_knowledge = "knowledge_base_ids" in data
    has_connectors = "mcp_servers" in data
    if has_knowledge or has_connectors:
        return (
            normalize_id_list(data.get("knowledge_base_ids")) if has_knowledge else [],
            normalize_id_list(data.get("mcp_servers")) if has_connectors else [],
        )
    if source_defaults is None:
        return [], []
    return (
        normalize_id_list(source_defaults[0]),
        normalize_id_list(source_defaults[1]),
    )


def resolve_installer_knowledge_bases(refs: list[str], visible_ids: set[str]) -> list[str]:
    """Keep template knowledge bases the installer can already see, in template order."""
    out: list[str] = []
    for raw in refs:
        if not isinstance(raw, str):
            continue
        kb_id = raw.strip()
        if kb_id and kb_id in visible_ids:
            out.append(kb_id)
    return list(dict.fromkeys(out))


def resolve_installer_mcp_servers(
    refs: list[str],
    *,
    allowed_names: set[str],
    names_by_kind: dict[str, list[str]],
    is_catalog_kind: Callable[[str], bool],
) -> list[str]:
    """Map template connector refs onto instances the installer already has.

    An exact server name is kept only when this user can already use it.
    Otherwise a catalog kind (or kind__instance from the publisher) expands
    to this user's own instances of that kind. Kinds they have not installed,
    and custom servers they cannot already see, are omitted.
    """
    out: list[str] = []
    for raw in refs:
        if not isinstance(raw, str):
            continue
        name = raw.strip()
        if not name:
            continue
        if name in allowed_names:
            out.append(name)
            continue
        kind = name.split("__", 1)[0] if "__" in name else name
        if not is_catalog_kind(kind):
            continue
        for owned in names_by_kind.get(kind, []):
            if owned in allowed_names:
                out.append(owned)
    return list(dict.fromkeys(out))


async def strip_seeded_composer_defaults(workspace: Any) -> None:
    """Remove publisher resource ids from the copy seeded into a new workspace."""
    text = await read_workspace_manifest_text(workspace)
    if text is None or not str(text).strip():
        return
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return
    if not isinstance(parsed, dict):
        return
    if not any(key in parsed for key in COMPOSER_DEFAULT_MANIFEST_KEYS):
        return
    for key in COMPOSER_DEFAULT_MANIFEST_KEYS:
        parsed.pop(key, None)
    payload = json.dumps(parsed, ensure_ascii=False, indent=2).encode("utf-8")
    await workspace.aupload_many([(workspace_manifest_rel(workspace), payload)])
