"""Integration tests for non-admin per-agent sandbox isolation across API routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.support.auth import TEST_PASSWORD, create_user


@pytest.mark.asyncio
async def test_sandbox_isolation_confines_filesystem_and_agent_creation_even_in_container(
    env: tuple[httpx.AsyncClient, Any, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from octop.infra.backend.sandbox import agent_sandbox_root, user_sandbox_root

    client, srv, admin_auth = env
    monkeypatch.setenv("OCTOP_AGENT_SANDBOX_ISOLATION", "1")
    monkeypatch.setenv("OCTOP_IN_CONTAINER", "1")

    outside = tmp_path / "outside-dir"
    outside.mkdir()

    staff_auth = await create_user(client, admin_auth, username="staff_sb", password=TEST_PASSWORD)
    users = (await client.get("/api/users", headers=admin_auth)).json()
    staff_id = next(u["id"] for u in users if u["username"] == "staff_sb")
    expected_user_root = user_sandbox_root(srv.paths, staff_id, ensure=False)

    # Non-admin filesystem endpoints are confined to user_sandbox_root even inside container.
    defaults = await client.get("/api/filesystem/defaults", headers=staff_auth)
    assert defaults.status_code == 200, defaults.text
    body = defaults.json()
    assert Path(body["default_root_dir"]).resolve() == expected_user_root
    assert Path(body["tree_root"]).resolve() == expected_user_root
    assert [Path(p).resolve() for p in body["browse_roots"]] == [expected_user_root]
    assert body["in_container"] is True

    outside_dirs = await client.get(
        f"/api/filesystem/dirs?path={outside.as_posix()}",
        headers=staff_auth,
    )
    assert outside_dirs.status_code == 400, outside_dirs.text

    # Admin still sees host defaults.
    admin_defaults = await client.get("/api/filesystem/defaults", headers=admin_auth)
    assert admin_defaults.status_code == 200, admin_defaults.text
    assert Path(admin_defaults.json()["default_root_dir"]).resolve() != expected_user_root

    # Creating an agent with root_dir="/" snaps to per-agent sandbox root.
    created = await client.post(
        "/api/agents",
        headers=staff_auth,
        json={
            "name": "Staff Sandbox Agent",
            "config": {"backend": {"type": "local_shell", "root_dir": "/", "virtual_mode": True}},
        },
    )
    assert created.status_code == 201, created.text
    agent_id = created.json()["agent_id"]
    detail = (await client.get(f"/api/agents/{agent_id}", headers=staff_auth)).json()
    expected_agent_root = agent_sandbox_root(srv.paths, staff_id, agent_id, ensure=False).as_posix()
    assert Path(detail["config"]["backend"]["root_dir"]).as_posix() == expected_agent_root

    # Attempting to create with an outside root_dir is rejected.
    rejected = await client.post(
        "/api/agents",
        headers=staff_auth,
        json={
            "name": "Escape Attempt",
            "config": {
                "backend": {
                    "type": "local_shell",
                    "root_dir": outside.as_posix(),
                    "virtual_mode": True,
                }
            },
        },
    )
    assert rejected.status_code in {400, 403}, rejected.text

    # Admin publishes a template; staff installs it from market and gets isolated sandbox root.
    admin_src = await client.post(
        "/api/agents",
        headers=admin_auth,
        json={"name": "MCN Base Template"},
    )
    assert admin_src.status_code == 201, admin_src.text
    src_id = admin_src.json()["agent_id"]

    published = await client.post(
        f"/api/agents/{src_id}/publish-expert",
        headers=admin_auth,
        json={"name": "MCN Market Expert", "description": "Shared MCN template"},
    )
    assert published.status_code == 201, published.text
    pub_id = published.json()["id"]

    installed = await client.post(
        f"/api/experts/published/{pub_id}/install",
        headers=staff_auth,
        json={
            "name": "My Private MCN",
            "backend": {"type": "local_shell", "root_dir": "/", "virtual_mode": True},
        },
    )
    assert installed.status_code == 201, installed.text
    inst_id = installed.json()["agent_id"]
    inst_detail = (await client.get(f"/api/agents/{inst_id}", headers=staff_auth)).json()
    expected_inst_root = agent_sandbox_root(srv.paths, staff_id, inst_id, ensure=False).as_posix()
    assert Path(inst_detail["config"]["backend"]["root_dir"]).as_posix() == expected_inst_root
