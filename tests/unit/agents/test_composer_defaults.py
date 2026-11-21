"""Published-expert composer defaults stay inside the installer's own access."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from deepagents.backends.local_shell import LocalShellBackend
from octop_harness.backends.workspace import BackendWorkspace

from octop.infra.agents.experts.catalog import WORKSPACE_MANIFEST_PATH
from octop.infra.agents.experts.composer_defaults import (
    resolve_installer_knowledge_bases,
    resolve_installer_mcp_servers,
    strip_seeded_composer_defaults,
    template_composer_defaults,
)


def _catalog(kind: str) -> bool:
    return kind == "tencent-docs"


def test_template_defaults_prefer_snapshot_and_fall_back_when_absent() -> None:
    assert template_composer_defaults(
        {"knowledge_base_ids": ["kb-private", " kb-shared ", "kb-private"], "mcp_servers": []},
        source_defaults=(["live-kb"], ["live-mcp"]),
    ) == (["kb-private", "kb-shared"], [])
    assert template_composer_defaults(
        {"welcome_message": {"zh": "hi"}},
        source_defaults=(["live-kb"], ["live-mcp"]),
    ) == (["live-kb"], ["live-mcp"])
    assert template_composer_defaults({}) == ([], [])
    assert template_composer_defaults(
        {"knowledge_base_ids": ["ok", 3, ""], "mcp_servers": [" connector "]}
    ) == (["ok"], ["connector"])


def test_installer_only_keeps_visible_knowledge_bases() -> None:
    assert resolve_installer_knowledge_bases(
        ["private", "shared", "private", "missing"],
        {"shared"},
    ) == ["shared"]


def test_installer_connectors_use_own_instances_and_drop_unknown_kinds() -> None:
    resolved = resolve_installer_mcp_servers(
        [
            "tencent-docs__publisher",
            "shared-server",
            "custom__other__secret",
            "not-a-kind",
        ],
        allowed_names={"tencent-docs__mine", "shared-server"},
        names_by_kind={"tencent-docs": ["tencent-docs__mine", "tencent-docs__other"]},
        is_catalog_kind=_catalog,
    )
    assert resolved == ["tencent-docs__mine", "shared-server"]
    assert "tencent-docs__publisher" not in resolved
    assert resolve_installer_mcp_servers(
        ["tencent-docs__publisher"],
        allowed_names=set(),
        names_by_kind={},
        is_catalog_kind=_catalog,
    ) == []


@pytest.mark.asyncio
async def test_seeded_workspace_manifest_drops_publisher_resource_ids(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    workspace = BackendWorkspace(
        LocalShellBackend(root_dir=str(root), virtual_mode=False),
        root,
    )
    manifest = {
        "id": "expert",
        "welcome_message": {"zh": "你好", "en": "Hello"},
        "knowledge_base_ids": ["private-kb"],
        "mcp_servers": ["tencent-docs__publisher"],
    }
    await workspace.aupload_many(
        [(WORKSPACE_MANIFEST_PATH, json.dumps(manifest, ensure_ascii=False).encode())]
    )

    await strip_seeded_composer_defaults(workspace)

    text = await workspace.aread_text(WORKSPACE_MANIFEST_PATH)
    assert text is not None
    assert "private-kb" not in text
    assert "tencent-docs__publisher" not in text
    saved = json.loads(text)
    assert saved["welcome_message"]["zh"] == "你好"
    assert "knowledge_base_ids" not in saved
    assert "mcp_servers" not in saved
