"""Tests for Huihuo ERP org projection, pre-login grants, and unified resource scope."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from octop.api.common.agent import assert_agent_access_row
from octop.api.routers import erp_org as erp_org_router
from octop.api.routers import skill_packages as skill_packages_router
from octop.config import OctopConfig, YouliConfig
from octop.infra.auth.youli.client import YouliClient
from octop.infra.auth.youli.org_sync import ErpOrgSyncService
from octop.infra.db.migrate import _split_pg_sql, run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import RepoBundle, SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.knowledge.service import KnowledgeService
from octop.infra.users.groups import (
    RESOURCE_EXPERT,
    RESOURCE_KNOWLEDGE_BASE,
    RESOURCE_SKILL_PACKAGE,
    VISIBILITY_COMPANY,
    VISIBILITY_SCOPED,
    GroupAccess,
)
from octop.infra.users.identity import User
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def repos(tmp_path: Path) -> RepoBundle:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return RepoBundle.from_pool(pool)


def _create_user(repos: RepoBundle, username: str, role: str = "user") -> int:
    return repos.user_repo.create(username=username, role=role)


def _bind_youli_session(
    repos: RepoBundle,
    *,
    user_id: int,
    company_id: int,
    employee_id: int,
    sid: str,
) -> None:
    with repos.db.transaction() as conn:
        conn.execute(
            "INSERT INTO youli_session("
            "sid, user_id, account_user_id, account_username, company_id, employee_id, "
            "industry, token_blob, token_expires_at, abs_expires_at, status, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, 6, NULL, 9999999999, 9999999999, 'active', 1000)",
            (sid, user_id, employee_id, f"emp_{employee_id}", company_id, employee_id),
        )


def test_migration_038_sqlite_and_pg_sql(repos: RepoBundle) -> None:
    with repos.db.connect() as conn:
        version = conn.execute("SELECT version FROM _schema_version").fetchone()[0]
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert version >= 38
    assert {
        "erp_departments",
        "erp_department_closure",
        "erp_directory_employees",
        "erp_employee_departments",
        "resource_scope_policy",
        "resource_subject_grant",
    }.issubset(tables)

    pg_path = (
        Path(__file__).resolve().parents[3]
        / "src/octop/infra/db/migrations/038_erp_org_and_resource_grants.pg.sql"
    )
    stmts = _split_pg_sql(pg_path.read_text(encoding="utf-8"))
    assert len(stmts) >= 12
    assert "UPDATE _schema_version SET version = 38" in stmts[-1]


def test_org_sync_closure_multi_dept_and_resign_revocation(repos: RepoBundle) -> None:
    alice = _create_user(repos, "alice")
    bob = _create_user(repos, "bob")
    _bind_youli_session(repos, user_id=alice, company_id=100, employee_id=1001, sid="sid-alice")
    _bind_youli_session(repos, user_id=bob, company_id=100, employee_id=1002, sid="sid-bob")

    depts = [
        {"orgId": 10, "parentId": 1, "name": "会火总公司", "orgCode": "ROOT", "sort": 1, "status": 1},
        {"orgId": 20, "parentId": 10, "name": "内容中心", "orgCode": "CONTENT", "sort": 2, "status": 1},
        {"orgId": 30, "parentId": 20, "name": "短视频组", "orgCode": "VIDEO", "sort": 3, "status": 1},
        {"orgId": 40, "parentId": 10, "name": "财务部", "orgCode": "FIN", "sort": 4, "status": 1},
    ]
    emps = [
        {
            "employeeId": 1001,
            "accountUserId": 9001,
            "nickName": "Alice",
            "phone": "13800000001",
            "employeeNo": "HH001",
            "status": 1,
            "isAdmin": 0,
            "orgIds": [30, 40],
            "adminOrgIds": [30],
        },
        {
            "employeeId": 1002,
            "accountUserId": 9002,
            "nickName": "Bob",
            "phone": "13800000002",
            "employeeNo": "HH002",
            "status": 2,  # resigned
            "isAdmin": 0,
            "orgIds": [20],
            "adminOrgIds": [],
        },
        {
            "employeeId": 1003,
            "accountUserId": 9003,
            "nickName": "Charlie-Unlogged",
            "phone": "13800000003",
            "employeeNo": "HH003",
            "status": 1,
            "isAdmin": 0,
            "orgIds": [30],
            "adminOrgIds": [],
        },
    ]

    summary = repos.erp_org_repo.sync_company_directory(100, depts, emps)
    assert summary["department_count"] == 4
    assert summary["employee_count"] == 3
    assert summary["active_employee_count"] == 2
    assert summary["revoked_user_ids"] == [bob]

    alice_ctx = repos.erp_org_repo.get_user_erp_context(alice)
    assert alice_ctx is not None
    assert alice_ctx.active_in_erp is True
    assert alice_ctx.direct_org_ids == frozenset({30, 40})
    assert alice_ctx.ancestor_org_ids == frozenset({10, 20, 30, 40})
    assert alice_ctx.admin_org_ids == frozenset({30})

    bob_ctx = repos.erp_org_repo.get_user_erp_context(bob)
    assert bob_ctx is not None
    assert bob_ctx.active_in_erp is False
    assert bob_ctx.direct_org_ids == frozenset()

    # Pre-login employee appears in directory with has_octop_account=False.
    listed = repos.erp_org_repo.list_employees(company_id=100, org_id=20, include_children=True)
    listed_ids = {item["employee_id"]: item for item in listed}
    assert set(listed_ids) == {1001, 1003}
    assert listed_ids[1001]["has_octop_account"] is True
    assert listed_ids[1003]["has_octop_account"] is False
    assert listed_ids[1003]["org_names"] == ["短视频组"]


def test_prelogin_erp_employee_grant_and_department_include_children_and_deny(
    repos: RepoBundle,
) -> None:
    admin = _create_user(repos, "root", role="admin")
    child_user = _create_user(repos, "child_u")
    parent_only_user = _create_user(repos, "parent_u")
    outsider_company_user = _create_user(repos, "other_co")
    local_only_user = _create_user(repos, "local_u")

    _bind_youli_session(repos, user_id=child_user, company_id=100, employee_id=201, sid="s-c")
    _bind_youli_session(
        repos, user_id=parent_only_user, company_id=100, employee_id=202, sid="s-p"
    )
    _bind_youli_session(
        repos, user_id=outsider_company_user, company_id=200, employee_id=301, sid="s-o"
    )

    repos.erp_org_repo.sync_company_directory(
        100,
        [
            {"orgId": 10, "parentId": 1, "name": "总经办"},
            {"orgId": 20, "parentId": 10, "name": "运营部"},
            {"orgId": 30, "parentId": 20, "name": "实习生组"},
        ],
        [
            {"employeeId": 201, "nickName": "小李", "status": 1, "orgIds": [30]},
            {"employeeId": 202, "nickName": "王总", "status": 1, "orgIds": [20]},
            {"employeeId": 203, "nickName": "未登录新员工", "status": 1, "orgIds": [10]},
        ],
    )
    repos.erp_org_repo.sync_company_directory(
        200,
        [{"orgId": 20, "parentId": 1, "name": "外部公司运营部"}],
        [{"employeeId": 301, "nickName": "外司", "status": 1, "orgIds": [20]}],
    )

    access = GroupAccess(repos)

    # 1) Non-admin cannot assign extended scope.
    with pytest.raises(OctopError) as exc_info:
        access.validate_scope(
            visibility=VISIBILITY_SCOPED,
            department_grants=[{"company_id": 100, "org_id": 20, "include_children": True}],
            actor_user_id=child_user,
            actor_is_admin=False,
        )
    assert exc_info.value.code == ErrorCode.FORBIDDEN

    # 2) Dept 20 (include_children=True), deny Dept 30, allow pre-login emp 203 + local_only_user.
    scope = access.validate_scope(
        visibility=VISIBILITY_SCOPED,
        company_id=0,
        department_grants=[{"company_id": 100, "org_id": 20, "include_children": True}],
        erp_employees=[{"company_id": 100, "employee_id": 203}],
        user_ids=[local_only_user],
        deny_department_grants=[{"company_id": 100, "org_id": 30, "include_children": True}],
        actor_user_id=admin,
        actor_is_admin=True,
    )
    access.set_scope(RESOURCE_SKILL_PACKAGE, "pkg-1", scope, actor_user_id=admin)

    # parent_only_user (dept 20) -> allowed
    assert access.allows(
        user_id=parent_only_user,
        is_admin=False,
        resource_type=RESOURCE_SKILL_PACKAGE,
        resource_key="pkg-1",
        is_owner=False,
    )
    # child_user (dept 30) -> denied because deny_department_grants overrides allow!
    assert not access.allows(
        user_id=child_user,
        is_admin=False,
        resource_type=RESOURCE_SKILL_PACKAGE,
        resource_key="pkg-1",
        is_owner=False,
    )
    # outsider_company_user (company 200, same org_id=20) -> denied by company_id isolation!
    assert not access.allows(
        user_id=outsider_company_user,
        is_admin=False,
        resource_type=RESOURCE_SKILL_PACKAGE,
        resource_key="pkg-1",
        is_owner=False,
    )
    # local_only_user -> allowed via direct user_id grant
    assert access.allows(
        user_id=local_only_user,
        is_admin=False,
        resource_type=RESOURCE_SKILL_PACKAGE,
        resource_key="pkg-1",
        is_owner=False,
    )

    # 3) Pre-login employee 203 logs into Octop for the first time -> pre-grant activates!
    charlie = _create_user(repos, "charlie_first_login")
    repos.erp_org_repo.link_user(100, 203, charlie)
    assert access.allows(
        user_id=charlie,
        is_admin=False,
        resource_type=RESOURCE_SKILL_PACKAGE,
        resource_key="pkg-1",
        is_owner=False,
    )

    # 4) Dept include_children=False only matches direct members of dept 20, not child dept 30.
    direct_only = access.validate_scope(
        visibility=VISIBILITY_SCOPED,
        company_id=100,
        department_grants=[{"company_id": 100, "org_id": 20, "include_children": False}],
        actor_user_id=admin,
        actor_is_admin=True,
    )
    access.set_scope(RESOURCE_SKILL_PACKAGE, "pkg-direct", direct_only, actor_user_id=admin)
    assert access.allows(
        user_id=parent_only_user,
        is_admin=False,
        resource_type=RESOURCE_SKILL_PACKAGE,
        resource_key="pkg-direct",
        is_owner=False,
    )
    assert not access.allows(
        user_id=child_user,
        is_admin=False,
        resource_type=RESOURCE_SKILL_PACKAGE,
        resource_key="pkg-direct",
        is_owner=False,
    )

    # 5) Company visibility only permits active members of company 100.
    co_scope = access.validate_scope(
        visibility=VISIBILITY_COMPANY,
        company_id=100,
        actor_user_id=admin,
        actor_is_admin=True,
    )
    access.set_scope(RESOURCE_SKILL_PACKAGE, "pkg-co", co_scope, actor_user_id=admin)
    assert access.allows(
        user_id=child_user,
        is_admin=False,
        resource_type=RESOURCE_SKILL_PACKAGE,
        resource_key="pkg-co",
        is_owner=False,
    )
    assert not access.allows(
        user_id=outsider_company_user,
        is_admin=False,
        resource_type=RESOURCE_SKILL_PACKAGE,
        resource_key="pkg-co",
        is_owner=False,
    )
    assert not access.allows(
        user_id=local_only_user,
        is_admin=False,
        resource_type=RESOURCE_SKILL_PACKAGE,
        resource_key="pkg-co",
        is_owner=False,
    )


def test_fail_closed_when_scoped_grants_removed_or_dept_disabled(repos: RepoBundle) -> None:
    admin = _create_user(repos, "root", role="admin")
    staff = _create_user(repos, "staff")
    _bind_youli_session(repos, user_id=staff, company_id=100, employee_id=401, sid="s-staff")

    repos.erp_org_repo.sync_company_directory(
        100,
        [{"orgId": 50, "parentId": 1, "name": "设计部"}],
        [{"employeeId": 401, "nickName": "设计师", "status": 1, "orgIds": [50]}],
    )
    access = GroupAccess(repos)
    scope = access.validate_scope(
        visibility=VISIBILITY_SCOPED,
        company_id=100,
        department_grants=[{"company_id": 100, "org_id": 50, "include_children": True}],
        actor_user_id=admin,
        actor_is_admin=True,
    )
    access.set_scope(RESOURCE_EXPERT, "exp-design", scope, actor_user_id=admin)
    assert access.allows(
        user_id=staff,
        is_admin=False,
        resource_type=RESOURCE_EXPERT,
        resource_key="exp-design",
        is_owner=False,
    )

    # If the department is later omitted/disabled in ERP sync -> Fail-Closed!
    repos.erp_org_repo.sync_company_directory(
        100,
        [],
        [{"employeeId": 401, "nickName": "设计师", "status": 1, "orgIds": []}],
    )
    assert not access.allows(
        user_id=staff,
        is_admin=False,
        resource_type=RESOURCE_EXPERT,
        resource_key="exp-design",
        is_owner=False,
    )


def test_expert_kb_repo_filtering_loss_hides_and_kb_gap_sync(
    repos: RepoBundle, tmp_path: Path
) -> None:
    admin = _create_user(repos, "root", role="admin")
    alice = _create_user(repos, "alice")
    _bind_youli_session(repos, user_id=alice, company_id=100, employee_id=501, sid="s-alice")

    repos.erp_org_repo.sync_company_directory(
        100,
        [
            {"orgId": 10, "parentId": 1, "name": "商务部"},
            {"orgId": 20, "parentId": 1, "name": "法务部"},
        ],
        [{"employeeId": 501, "nickName": "Alice", "status": 1, "orgIds": [10]}],
    )

    # Create shared expert & KBs
    repos.agent_repo.create(
        agent_id="exp-biz",
        name="商务专家",
        user_id=admin,
    )
    repos.agent_repo.set_shared("exp-biz", True)
    expert = repos.agent_repo.get("exp-biz")
    assert expert is not None

    repos.agent_repo.create(
        agent_id="exp-legacy",
        name="老全员专家",
        user_id=admin,
    )
    repos.agent_repo.set_shared("exp-legacy", True)
    kb_private = repos.knowledge_repo.create_base(
        owner_user_id=admin, name="私有商务底稿", shared=False
    )
    kb_legal = repos.knowledge_repo.create_base(
        owner_user_id=admin, name="法务知识库", shared=True
    )

    access = GroupAccess(repos)
    legal_scope = access.validate_scope(
        visibility=VISIBILITY_SCOPED,
        company_id=100,
        department_grants=[{"company_id": 100, "org_id": 20, "include_children": True}],
        actor_user_id=admin,
        actor_is_admin=True,
    )
    access.set_scope(RESOURCE_KNOWLEDGE_BASE, kb_legal.id, legal_scope, actor_user_id=admin)

    biz_scope = access.validate_scope(
        visibility=VISIBILITY_SCOPED,
        company_id=100,
        department_grants=[{"company_id": 100, "org_id": 10, "include_children": True}],
        actor_user_id=admin,
        actor_is_admin=True,
    )
    access.set_scope(RESOURCE_EXPERT, expert.agent_id, biz_scope, actor_user_id=admin)

    # Alice sees exp-biz and legacy open expert, does NOT see kb_private or kb_legal yet.
    shared_ids = {r.agent_id for r in repos.agent_repo.list_shared(viewer_user_id=alice)}
    assert shared_ids == {"exp-biz", "exp-legacy"}
    visible_kbs = {r.id for r in repos.knowledge_repo.list_visible(alice)}
    assert visible_kbs == set()

    class _MockFakeUser:
        def __init__(self, uid: int, is_admin: bool = False) -> None:
            self.id = uid
            self.is_admin = is_admin

    assert_agent_access_row(expert, _MockFakeUser(alice), repos=repos)

    # Check KB gap detection & sync when admin confirms linking KBs to expert scope.
    gaps = access.check_agent_kb_gaps(
        kb_ids=[kb_private.id, kb_legal.id, "private-feishu:src1"],
        scope=biz_scope,
    )
    assert {g["kb_id"] for g in gaps} == {kb_private.id, kb_legal.id}

    synced = access.sync_agent_kb_grants(
        kb_ids=[kb_private.id, kb_legal.id, "private-feishu:src1"],
        scope=biz_scope,
        actor_user_id=admin,
    )
    assert set(synced) == {kb_private.id, kb_legal.id}

    # Alice now sees both synced KBs, and private KB stayed scoped (never became "all").
    visible_kbs_after = {r.id for r in repos.knowledge_repo.list_visible(alice)}
    assert visible_kbs_after == {kb_private.id, kb_legal.id}
    assert (
        access.scope_payload(RESOURCE_KNOWLEDGE_BASE, kb_private.id, shared=True)["visibility"]
        == VISIBILITY_SCOPED
    )

    # Department transfer: Alice moves out of dept 10 -> exp-biz & KBs hidden immediately!
    repos.erp_org_repo.sync_company_directory(
        100,
        [
            {"orgId": 10, "parentId": 1, "name": "商务部"},
            {"orgId": 20, "parentId": 1, "name": "法务部"},
        ],
        [{"employeeId": 501, "nickName": "Alice", "status": 1, "orgIds": []}],
    )
    shared_after_transfer = {
        r.agent_id for r in repos.agent_repo.list_shared(viewer_user_id=alice)
    }
    assert shared_after_transfer == {"exp-legacy"}
    with pytest.raises(OctopError) as access_exc:
        assert_agent_access_row(expert, _MockFakeUser(alice), repos=repos)
    assert access_exc.value.code == ErrorCode.FORBIDDEN
    assert repos.knowledge_repo.list_visible(alice) == []

    # KnowledgeService.get_readable_base also enforces the lost dept permission.
    services = SharedServices(
        paths=PathLayout(tmp_path),
        config=OctopConfig(),
        repos=repos,
    )
    kb_svc = KnowledgeService(services)
    with pytest.raises(PermissionError):
        kb_svc.get_readable_base(kb_legal.id, actor_user_id=alice, is_admin=False)


@pytest.mark.asyncio
async def test_erp_org_router_and_skill_package_scope_endpoints(
    repos: RepoBundle, tmp_path: Path
) -> None:
    admin = _create_user(repos, "root", role="admin")
    staff = _create_user(repos, "staff")
    outsider = _create_user(repos, "outsider")
    _bind_youli_session(repos, user_id=staff, company_id=100, employee_id=801, sid="s-801")
    repos.erp_org_repo.sync_company_directory(
        100,
        [
            {"orgId": 10, "parentId": 1, "name": "总公司"},
            {"orgId": 20, "parentId": 10, "name": "内容部"},
        ],
        [{"employeeId": 801, "nickName": "内容专员", "status": 1, "orgIds": [20]}],
    )

    services = SharedServices(
        paths=PathLayout(tmp_path),
        config=OctopConfig(),
        repos=repos,
    )

    class _FakeServer:
        def __init__(self) -> None:
            self.services = services
            self.paths = services.paths
            self.app_runtime = None

    server = _FakeServer()
    admin_user = User(
        id=admin, username="root", role="admin", display_name="Root", locale="zh", permissions=["skill_packages"]
    )
    staff_user = User(
        id=staff, username="staff", role="user", display_name="Staff", locale="zh", permissions=["skill_packages"]
    )
    outsider_user = User(
        id=outsider, username="outsider", role="user", display_name="Outsider", locale="zh", permissions=["skill_packages"]
    )

    depts_out = await erp_org_router.list_erp_departments(
        company_id=100,
        include_inactive=False,
        _=admin_user,
        server=server,
    )
    assert len(depts_out["departments"]) == 2
    assert len(depts_out["tree"]) == 1
    assert depts_out["tree"][0]["children"][0]["org_id"] == 20

    emps_out = await erp_org_router.list_erp_employees(
        company_id=100,
        org_id=10,
        include_children=True,
        q="",
        active_only=True,
        _=admin_user,
        server=server,
    )
    assert len(emps_out["employees"]) == 1
    assert emps_out["employees"][0]["employee_id"] == 801

    local_users = await erp_org_router.list_local_users(q="staff", _=admin_user, server=server)
    assert [u["user_id"] for u in local_users["users"]] == [staff]

    # Non-admin cannot create a scoped skill package.
    with pytest.raises(OctopError) as non_admin_exc:
        await skill_packages_router.create_skill_package(
            skill_packages_router.CreateSkillPackageBody(
                name="受限技能",
                visibility="scoped",
                company_id=100,
                department_grants=[{"company_id": 100, "org_id": 20, "include_children": True}],
            ),
            server=server,  # type: ignore[arg-type]
            user=staff_user,
        )
    assert non_admin_exc.value.code == ErrorCode.FORBIDDEN

    # Admin creates a scoped skill package -> visible to dept 20 staff, hidden from outsider.
    created = await skill_packages_router.create_skill_package(
        skill_packages_router.CreateSkillPackageBody(
            name="内容创作SOP",
            visibility="scoped",
            company_id=100,
            department_grants=[{"company_id": 100, "org_id": 20, "include_children": True}],
        ),
        server=server,  # type: ignore[arg-type]
        user=admin_user,
    )
    assert created["visibility"] == "scoped"
    assert created["department_grants"] == [
        {"company_id": 100, "org_id": 20, "include_children": True}
    ]

    staff_list = await skill_packages_router.list_skill_packages(
        server=server,  # type: ignore[arg-type]
        user=staff_user,
    )
    assert [p["id"] for p in staff_list] == [created["id"]]

    outsider_list = await skill_packages_router.list_skill_packages(
        server=server,  # type: ignore[arg-type]
        user=outsider_user,
    )
    assert outsider_list == []


def test_youli_client_fetch_org_directory_and_sync_revokes_session(
    repos: RepoBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    emp_user = _create_user(repos, "emp_user")
    _bind_youli_session(repos, user_id=emp_user, company_id=100, employee_id=777, sid="sid-777")

    cfg = YouliConfig(
        enabled=True,
        base_url="https://erp.example.com",
        client_id="octop",
        client_secret="secret",
        audience="octop",
    )

    def _fake_perform(_cls: Any, method: str, url: str, **kwargs: Any) -> httpx.Response:
        assert method == "POST"
        assert url == "https://erp.example.com/api/crm/sso/youli/org-directory"
        body = kwargs["json"]
        assert body["companyId"] == 100
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "companyId": 100,
                    "syncedAtEpoch": 1700000000,
                    "departments": [
                        {"orgId": 10, "parentId": 1, "name": "总公司", "orgCode": "01", "sort": 1, "status": 1}
                    ],
                    "employees": [
                        {
                            "employeeId": 777,
                            "accountUserId": 9777,
                            "nickName": "离职员工",
                            "phone": "13900000777",
                            "employeeNo": "HH777",
                            "status": 2,
                            "isAdmin": 0,
                            "orgIds": [10],
                            "adminOrgIds": [],
                        }
                    ],
                },
            },
        )

    monkeypatch.setattr("octop.infra.auth.youli.http_pool.perform_request", _fake_perform)

    client = YouliClient(cfg)
    directory = client.fetch_org_directory(company_id=100)
    assert directory.company_id == 100
    assert len(directory.departments) == 1
    assert directory.employees[0].status == 2

    class _FakeServices:
        def __init__(self, r: RepoBundle) -> None:
            self.repos = r

    summary = ErpOrgSyncService(_FakeServices(repos), cfg).sync_company(100)
    assert summary["revoked_user_ids"] == [emp_user]
    assert repos.youli_session_repo.get_active("sid-777") is None


def test_known_company_ids_includes_youli_sso_identities(repos: RepoBundle) -> None:
    youli_provider = repos.sso_repo.upsert_by_kind(
        "youli",
        enabled=True,
        display_name="Youli ERP",
        issuer="https://erp.example",
        client_id="erp",
        client_secret_enc=None,
        scopes="",
        dashboard_origin=None,
        extra={},
    )
    emp_user = _create_user(repos, "legacy-youli-emp")
    repos.user_repo.upsert_sso_identity(
        emp_user,
        provider_id=youli_provider.id,
        subject="youli:8801:9002",
    )
    assert repos.erp_org_repo.known_company_ids() == [8801]
