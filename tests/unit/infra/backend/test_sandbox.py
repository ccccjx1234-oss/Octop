"""Unit tests for per-agent bwrap sandbox confinement and bwrap probe."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from octop.config import OctopConfig
from octop.infra.agents.manager import AgentCreateSpec, AgentManager
from octop.infra.agents.workspace.dir import remove_agent_host_dirs
from octop.infra.backend.sandbox import (
    agent_sandbox_root,
    sandbox_isolation_enabled,
    user_sandbox_root,
)
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.utils import bwrap as bwrap_mod
from octop.infra.utils.paths import PathLayout

_MEMORY_OFF = {"memory": {"memory_enabled": False}}


@pytest.fixture
def sandbox_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AgentManager, int, int]:
    monkeypatch.setenv("OCTOP_AGENT_SANDBOX_ISOLATION", "1")
    monkeypatch.setenv("OCTOP_IN_CONTAINER", "1")
    paths = PathLayout(tmp_path / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    admin_id = services.user_repo.create(username="admin", password_hash="hash", role="admin")
    staff_id = services.user_repo.create(username="staff", password_hash="hash", role="user")
    manager = AgentManager(repos=services.repos, paths=services.paths)
    monkeypatch.setattr(manager, "_schedule_reload", lambda _aid: None)
    return manager, admin_id, staff_id


def test_sandbox_isolation_env_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCTOP_AGENT_SANDBOX_ISOLATION", raising=False)
    assert sandbox_isolation_enabled() is False
    monkeypatch.setenv("OCTOP_AGENT_SANDBOX_ISOLATION", "1")
    assert sandbox_isolation_enabled() is True
    monkeypatch.setenv("OCTOP_AGENT_SANDBOX_ISOLATION", "0")
    assert sandbox_isolation_enabled() is False


@pytest.mark.asyncio
async def test_non_admin_create_snaps_host_root_and_user_root_to_agent_sandbox(
    sandbox_env: tuple[AgentManager, int, int],
) -> None:
    manager, _admin_id, staff_id = sandbox_env
    user_root = user_sandbox_root(manager.paths, staff_id)

    # Omitted backend -> confined to per-agent sandbox root.
    row_omitted = await manager.create(
        AgentCreateSpec(name="omitted", user_id=staff_id, config={**_MEMORY_OFF}),
        defer_bootstrap=True,
    )
    cfg_omitted = manager.get_config(row_omitted.agent_id)
    expected_root_1 = agent_sandbox_root(manager.paths, staff_id, row_omitted.agent_id, ensure=False)
    assert cfg_omitted["backend"]["root_dir"] == str(expected_root_1)
    assert cfg_omitted["workspace_dir"] == f"/.octop/workspaces/{row_omitted.agent_id}"

    # Host root "/" -> confined to per-agent sandbox root.
    row_slash = await manager.create(
        AgentCreateSpec(
            name="slash",
            user_id=staff_id,
            config={
                **_MEMORY_OFF,
                "backend": {"type": "local_shell", "root_dir": "/", "virtual_mode": True},
            },
        ),
        defer_bootstrap=True,
    )
    cfg_slash = manager.get_config(row_slash.agent_id)
    expected_root_2 = agent_sandbox_root(manager.paths, staff_id, row_slash.agent_id, ensure=False)
    assert cfg_slash["backend"]["root_dir"] == str(expected_root_2)

    # UI default user_root -> upgraded to per-agent sandbox root.
    row_ui = await manager.create(
        AgentCreateSpec(
            name="ui-default",
            user_id=staff_id,
            config={
                **_MEMORY_OFF,
                "backend": {"type": "local_shell", "root_dir": str(user_root), "virtual_mode": True},
            },
        ),
        defer_bootstrap=True,
    )
    cfg_ui = manager.get_config(row_ui.agent_id)
    expected_root_3 = agent_sandbox_root(manager.paths, staff_id, row_ui.agent_id, ensure=False)
    assert cfg_ui["backend"]["root_dir"] == str(expected_root_3)
    assert expected_root_1 != expected_root_2 != expected_root_3


@pytest.mark.asyncio
async def test_non_admin_allows_custom_subdir_within_user_sandbox_and_rejects_escape(
    sandbox_env: tuple[AgentManager, int, int],
    tmp_path: Path,
) -> None:
    manager, admin_id, staff_id = sandbox_env
    user_root = user_sandbox_root(manager.paths, staff_id)
    custom_sub = user_root / "team-project"
    custom_sub.mkdir()
    outside = tmp_path / "outside-secret"
    outside.mkdir()

    # Subdirectory inside user_root is allowed.
    row_sub = await manager.create(
        AgentCreateSpec(
            name="subdir",
            user_id=staff_id,
            config={
                **_MEMORY_OFF,
                "backend": {"type": "local_shell", "root_dir": str(custom_sub), "virtual_mode": True},
            },
        ),
        defer_bootstrap=True,
    )
    assert manager.get_config(row_sub.agent_id)["backend"]["root_dir"] == str(custom_sub)

    # Outside path is rejected.
    with pytest.raises(OctopError) as exc_info:
        await manager.create(
            AgentCreateSpec(
                name="escape",
                user_id=staff_id,
                config={
                    **_MEMORY_OFF,
                    "backend": {"type": "local_shell", "root_dir": str(outside), "virtual_mode": True},
                },
            ),
            defer_bootstrap=True,
        )
    assert exc_info.value.code is ErrorCode.WORKSPACE_ROOT_RESTRICTED

    # Admin retains host path access.
    row_admin = await manager.create(
        AgentCreateSpec(
            name="admin-host",
            user_id=admin_id,
            config={
                **_MEMORY_OFF,
                "backend": {"type": "local_shell", "root_dir": str(outside), "virtual_mode": True},
            },
        ),
        defer_bootstrap=True,
    )
    assert manager.get_config(row_admin.agent_id)["backend"]["root_dir"] == str(outside)


@pytest.mark.asyncio
async def test_update_prevents_sandbox_escape_and_keeps_legacy_metadata_editable(
    sandbox_env: tuple[AgentManager, int, int],
    tmp_path: Path,
) -> None:
    manager, _admin_id, staff_id = sandbox_env
    row = await manager.create(
        AgentCreateSpec(name="sandboxed", user_id=staff_id, config={**_MEMORY_OFF}),
        defer_bootstrap=True,
    )
    cfg = manager.get_config(row.agent_id)
    with pytest.raises(OctopError) as exc_info:
        await manager.update(
            row.agent_id,
            config_json=json.dumps(
                {**cfg, "backend": {"type": "local_shell", "root_dir": "/", "virtual_mode": True}}
            ),
        )
    assert exc_info.value.code is ErrorCode.WORKSPACE_ROOT_RESTRICTED

    # Legacy pre-sandbox agent under agents/<id> can still update non-backend config fields.
    legacy_ws = manager.paths.ensure_agent_workspace("LEG001")
    manager._repos.agent_repo.create(
        agent_id="LEG001",
        user_id=staff_id,
        name="legacy",
        config_json=json.dumps(
            {
                **_MEMORY_OFF,
                "workspace_dir": str(legacy_ws),
                "backend": {"type": "local_shell", "root_dir": "/", "virtual_mode": True},
            }
        ),
    )
    updated = await manager.update(
        "LEG001",
        config_json=json.dumps(
            {
                **_MEMORY_OFF,
                "enable_trajectory": False,
                "workspace_dir": str(legacy_ws),
                "backend": {"type": "local_shell", "root_dir": "/", "virtual_mode": True},
            }
        ),
    )
    assert manager.get_config(updated.agent_id)["enable_trajectory"] is False

    # But legacy agent cannot switch backend to a new outside path.
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(OctopError) as exc_out:
        await manager.update(
            "LEG001",
            config_json=json.dumps(
                {
                    **_MEMORY_OFF,
                    "workspace_dir": str(legacy_ws),
                    "backend": {"type": "local_shell", "root_dir": str(outside), "virtual_mode": True},
                }
            ),
        )
    assert exc_out.value.code is ErrorCode.WORKSPACE_ROOT_RESTRICTED


@pytest.mark.asyncio
async def test_remove_agent_host_dirs_cleans_sandbox_dir(
    sandbox_env: tuple[AgentManager, int, int],
) -> None:
    manager, _admin_id, staff_id = sandbox_env
    row = await manager.create(
        AgentCreateSpec(name="to-delete", user_id=staff_id, config={**_MEMORY_OFF}),
        defer_bootstrap=True,
    )
    cfg = manager.get_config(row.agent_id)
    sandbox_dir = agent_sandbox_root(manager.paths, staff_id, row.agent_id, ensure=False)
    assert sandbox_dir.is_dir()
    remove_agent_host_dirs(cfg, paths=manager.paths, agent_id=row.agent_id)
    assert not sandbox_dir.exists()


def test_bwrap_namespace_probe_detects_denied_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bwrap = tmp_path / "bwrap"
    fake_bwrap.write_text("", encoding="utf-8")
    monkeypatch.setattr(bwrap_mod.sys, "platform", "linux")
    monkeypatch.setattr(bwrap_mod.shutil, "which", lambda name: str(fake_bwrap) if name == "bwrap" else None)
    monkeypatch.setattr(
        bwrap_mod.subprocess,
        "run",
        lambda *_args, **_kw: subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="bwrap: Creating new namespace failed: Operation not permitted"
        ),
    )
    assert bwrap_mod.bwrap_namespace_supported(refresh=True) is False
    assert bwrap_mod.ensure_bubblewrap() == {
        "status": "degraded",
        "reason": "namespace_denied",
        "detail": "bubblewrap binary exists but container/host blocked namespace creation",
    }

    monkeypatch.setattr(
        bwrap_mod.subprocess,
        "run",
        lambda *_args, **_kw: subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )
    assert bwrap_mod.bwrap_namespace_supported(refresh=True) is True
    assert bwrap_mod.ensure_bubblewrap()["status"] == "ready"
