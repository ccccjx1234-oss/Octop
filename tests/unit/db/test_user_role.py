"""Role templates do not rewrite existing users."""

from __future__ import annotations

import json
from pathlib import Path

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.user_roles import BUILTIN_USER_ROLE_IDS, UserRoleRepo, parse_policies
from octop.infra.db.repos.users import UserRepo
from octop.infra.users.permissions import BASELINE_PERMISSIONS, YOULI_EMPLOYEE_PERMISSIONS


def test_seeded_roles_leave_existing_users_unchanged(tmp_path: Path) -> None:
    db = SqlitePool(tmp_path / "octop.db")
    run_migrations(db)
    users = UserRepo(db)
    uid = users.create(
        username="alice",
        password_hash="x",
        role="user",
        permissions=["browser"],
    )
    row = users.get(uid)
    assert row is not None
    assert row.role_name is None
    assert row.permissions == ["browser"]

    roles = UserRoleRepo(db)
    admin = roles.get("admin")
    user = roles.get("user")
    assert admin is not None and admin.user_role_name == "管理员"
    assert admin.policies == []
    assert user is not None
    assert set(user.permissions) == set(BASELINE_PERMISSIONS)
    assert user.policies == []
    employee = roles.get("youli_employee")
    assert employee is not None and employee.user_role_name == "会火员工"
    assert set(employee.permissions) == set(YOULI_EMPLOYEE_PERMISSIONS)
    assert "users" not in employee.permissions
    assert "browser" not in employee.permissions
    assert "knowledge_settings" not in employee.permissions
    assert frozenset({"admin", "user", "youli_employee"}) == BUILTIN_USER_ROLE_IDS

    # Built-in templates can be deleted at the repo layer, but migrate re-seeds them.
    assert roles.delete("user") is True
    roles.delete("admin")
    roles.delete("youli_employee")
    run_migrations(db)
    assert roles.get("admin") is not None
    assert roles.get("user") is not None
    assert roles.get("youli_employee") is not None
    again = users.get(uid)
    assert again is not None
    assert again.role_name is None
    assert again.permissions == ["browser"]
    assert again.role == "user"


def test_policies_array_omits_disabled_names() -> None:
    raw = json.dumps([{"name": "token_quota", "value": "10"}, {"name": "max_agents", "value": ""}])
    assert parse_policies(raw) == [("token_quota", "10")]


def test_role_repo_roundtrip(tmp_path: Path) -> None:
    db = SqlitePool(tmp_path / "octop.db")
    run_migrations(db)
    roles = UserRoleRepo(db)
    created = roles.create(
        user_role_name="分析师",
        description="分析角色",
        permissions=["browser"],
        policies=[("token_quota", "10")],
    )
    assert created.user_role_id
    assert created.user_role_id not in {"admin", "user"}
    loaded = roles.get_by_name("分析师")
    assert loaded is not None
    assert loaded.description == "分析角色"
    assert loaded.policies == [("token_quota", "10")]
    assert loaded.policy_value("max_agents") is None
    assert loaded.policy_value("workspace_root_dir") is None


def test_cli_and_seed_follow_preset_user_role(tmp_path: Path) -> None:
    from octop.cli.support.offline_ops import create_user_offline
    from octop.infra.db.repos.user_roles import seeded_user_role_assignment
    from octop.infra.users.permissions import BASELINE_PERMISSIONS

    home = tmp_path / "home"
    created = create_user_offline(
        username="cliuser",
        password="TestPass12",
        role="user",
        home=home,
    )
    assert created["role"] == "user"
    db = SqlitePool(home / "octop.db")
    row = UserRepo(db).get_by_username("cliuser")
    assert row is not None
    assert row.role == "user"
    assert row.role_name == "用户"
    assert set(row.permissions) == set(BASELINE_PERMISSIONS)

    assignment = seeded_user_role_assignment(db)
    assert assignment is not None
    assert assignment[0] == "user"
    db.close()


def test_youli_employee_old_snapshot_expands_once(tmp_path: Path) -> None:
    from octop.infra.db.migrate import _backfill_youli_employee_menu_permissions
    from octop.infra.db.repos.user_roles import youli_employee_role_assignment

    db = SqlitePool(tmp_path / "octop.db")
    run_migrations(db)
    users = UserRepo(db)
    old_id = users.create(
        username="old-employee",
        role="youli_employee",
        role_name="会火员工",
        permissions=["connector_builtin", "channels", "connector_enabled"],
    )
    custom_id = users.create(
        username="custom-employee",
        role="youli_employee",
        role_name="会火员工",
        permissions=["experts"],
    )
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM settings WHERE key = ?",
            ("youli_employee_menu_permissions_v1",),
        )
    _backfill_youli_employee_menu_permissions(db)

    old = users.get(old_id)
    custom = users.get(custom_id)
    assert old is not None and custom is not None
    assert set(old.permissions) == set(YOULI_EMPLOYEE_PERMISSIONS)
    assert custom.permissions == ["experts"]
    assignment = youli_employee_role_assignment(db)
    assert assignment[0] == "youli_employee"
    assert set(assignment[2]) == set(YOULI_EMPLOYEE_PERMISSIONS)
    db.close()
