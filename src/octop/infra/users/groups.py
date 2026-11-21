"""Unified resource visibility & authorization engine.

Supports legacy ``user_group`` grants (where no grant rows on a legacy resource
means open to everyone) alongside explicit enterprise scopes:
- ``owner``: private to creator/owner and admins
- ``all``: visible to all signed-in users (subject to optional deny rules)
- ``company``: visible to active ERP members of a specific ``company_id``
- ``groups``: narrowed to selected ``user_group`` ids (legacy compatible)
- ``scoped``: fail-closed whitelist over departments (with/without sub-depts),
  local users, pre-provisioned ERP employees, and user groups, with deny priority.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from octop.infra.errors import ErrorCode, OctopError

RESOURCE_SKILL_PACKAGE = "skill_package"
RESOURCE_KNOWLEDGE_BASE = "knowledge_base"
RESOURCE_CONNECTOR = "connector"
CONNECTOR_FEATURE_KEY = "feature"
RESOURCE_MODEL = "model"
RESOURCE_STORAGE_BACKEND = "storage_backend"
STORAGE_FEATURE_KEY = "feature"  # legacy feature-wide key; prefer backend name
RESOURCE_PLUGIN = "plugin"
RESOURCE_BRIDGE = "bridge"
RESOURCE_EXPERT = "expert"

RESOURCE_TYPES = frozenset(
    {
        RESOURCE_SKILL_PACKAGE,
        RESOURCE_KNOWLEDGE_BASE,
        RESOURCE_CONNECTOR,
        RESOURCE_MODEL,
        RESOURCE_STORAGE_BACKEND,
        RESOURCE_PLUGIN,
        RESOURCE_BRIDGE,
        RESOURCE_EXPERT,
    }
)

VISIBILITY_ALL = "all"
VISIBILITY_GROUPS = "groups"
VISIBILITY_OWNER = "owner"
VISIBILITY_COMPANY = "company"
VISIBILITY_SCOPED = "scoped"

VALID_VISIBILITIES = frozenset(
    {
        VISIBILITY_ALL,
        VISIBILITY_GROUPS,
        VISIBILITY_OWNER,
        VISIBILITY_COMPANY,
        VISIBILITY_SCOPED,
    }
)

SUBJECT_DEPARTMENT = "department"
SUBJECT_USER = "user"
SUBJECT_ERP_EMPLOYEE = "erp_employee"
SUBJECT_USER_GROUP = "user_group"

T = TypeVar("T")


def department_subject_id(company_id: int, org_id: int) -> str:
    return f"{int(company_id)}:{int(org_id)}"


def erp_employee_subject_id(company_id: int, employee_id: int) -> str:
    return f"{int(company_id)}:{int(employee_id)}"


def parse_scoped_pair(raw: str) -> tuple[int, int] | None:
    parts = str(raw or "").strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    left, right = int(parts[0]), int(parts[1])
    if left <= 0 or right <= 0:
        return None
    return left, right


@dataclass(frozen=True)
class NormalizedScope:
    visibility: str
    company_id: int
    group_ids: tuple[str, ...]
    department_grants: tuple[dict[str, Any], ...]
    user_ids: tuple[int, ...]
    erp_employees: tuple[dict[str, Any], ...]
    deny_department_grants: tuple[dict[str, Any], ...]
    deny_user_ids: tuple[int, ...]
    deny_erp_employees: tuple[dict[str, Any], ...]

    def to_grant_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for gid in self.group_ids:
            rows.append(
                {
                    "subject_type": SUBJECT_USER_GROUP,
                    "subject_id": gid,
                    "include_children": True,
                    "effect": "allow",
                    "permission_level": "use",
                }
            )
        for item in self.department_grants:
            rows.append(
                {
                    "subject_type": SUBJECT_DEPARTMENT,
                    "subject_id": department_subject_id(item["company_id"], item["org_id"]),
                    "include_children": bool(item.get("include_children", True)),
                    "effect": "allow",
                    "permission_level": "use",
                }
            )
        for uid in self.user_ids:
            rows.append(
                {
                    "subject_type": SUBJECT_USER,
                    "subject_id": str(uid),
                    "include_children": False,
                    "effect": "allow",
                    "permission_level": "use",
                }
            )
        for item in self.erp_employees:
            rows.append(
                {
                    "subject_type": SUBJECT_ERP_EMPLOYEE,
                    "subject_id": erp_employee_subject_id(
                        item["company_id"], item["employee_id"]
                    ),
                    "include_children": False,
                    "effect": "allow",
                    "permission_level": "use",
                }
            )
        for item in self.deny_department_grants:
            rows.append(
                {
                    "subject_type": SUBJECT_DEPARTMENT,
                    "subject_id": department_subject_id(item["company_id"], item["org_id"]),
                    "include_children": bool(item.get("include_children", True)),
                    "effect": "deny",
                    "permission_level": "use",
                }
            )
        for uid in self.deny_user_ids:
            rows.append(
                {
                    "subject_type": SUBJECT_USER,
                    "subject_id": str(uid),
                    "include_children": False,
                    "effect": "deny",
                    "permission_level": "use",
                }
            )
        for item in self.deny_erp_employees:
            rows.append(
                {
                    "subject_type": SUBJECT_ERP_EMPLOYEE,
                    "subject_id": erp_employee_subject_id(
                        item["company_id"], item["employee_id"]
                    ),
                    "include_children": False,
                    "effect": "deny",
                    "permission_level": "use",
                }
            )
        return rows

    def to_allow_grant_rows(self) -> list[dict[str, Any]]:
        return [item for item in self.to_grant_rows() if item["effect"] == "allow"]


def scope_fields_from_body(body: Any) -> dict[str, Any] | None:
    """Extract extended scope fields from a Pydantic request model if any were provided."""
    fields_set = getattr(body, "model_fields_set", None)
    extended_keys = {
        "company_id",
        "department_grants",
        "user_ids",
        "erp_employees",
        "deny_department_grants",
        "deny_user_ids",
        "deny_erp_employees",
    }
    visibility = getattr(body, "visibility", None)
    if fields_set is not None:
        has_extended = bool(fields_set & extended_keys) or visibility in {
            VISIBILITY_COMPANY,
            VISIBILITY_SCOPED,
        }
    else:
        has_extended = visibility in {VISIBILITY_COMPANY, VISIBILITY_SCOPED} or any(
            getattr(body, key, None) is not None for key in extended_keys
        )
    if not has_extended:
        return None
    return {
        "visibility": visibility,
        "group_ids": getattr(body, "group_ids", None),
        "company_id": getattr(body, "company_id", None),
        "department_grants": getattr(body, "department_grants", None),
        "user_ids": getattr(body, "user_ids", None),
        "erp_employees": getattr(body, "erp_employees", None),
        "deny_department_grants": getattr(body, "deny_department_grants", None),
        "deny_user_ids": getattr(body, "deny_user_ids", None),
        "deny_erp_employees": getattr(body, "deny_erp_employees", None),
    }


def model_resource_key(provider_name: str, model_id: str) -> str:
    return f"{provider_name}/{model_id}"


def model_ref_allowed(
    repos: Any,
    *,
    user_id: int,
    is_admin: bool,
    model_ref: str | None,
) -> bool:
    """Whether *model_ref* (``provider/model``) passes the group layer.

    Blank refs, ``auto``, and refs without a provider prefix are not model grants.
    """
    ref = (model_ref or "").strip()
    if not ref or ref.lower() == "auto" or "/" not in ref:
        return True
    return GroupAccess(repos).allows(
        user_id=user_id,
        is_admin=is_admin,
        resource_type=RESOURCE_MODEL,
        resource_key=ref,
        is_owner=False,
    )


def group_visibility_sql(resource_type: str, key_expr: str) -> str:
    """SQL fragment: no grants, or the bound user belongs to a granted group.

    ``resource_type`` and ``key_expr`` are fixed by the caller, not request data.
    The user id placeholder is ``?``.
    """
    if resource_type not in RESOURCE_TYPES:
        raise ValueError(f"unknown resource type {resource_type!r}")
    return (
        "NOT EXISTS ("
        " SELECT 1 FROM resource_group_grant g"
        f" WHERE g.resource_type = '{resource_type}' AND g.resource_key = {key_expr}"
        ") OR EXISTS ("
        " SELECT 1 FROM resource_group_grant g"
        " JOIN user_group_member m ON m.user_group_id = g.user_group_id"
        " JOIN user_group ug ON ug.user_group_id = g.user_group_id AND ug.enabled = 1"
        f" WHERE g.resource_type = '{resource_type}' AND g.resource_key = {key_expr}"
        " AND m.user_id = ?"
        ")"
    )


def _evaluate_access(
    *,
    user_id: int,
    is_admin: bool,
    is_owner: bool,
    policy: Any | None,
    subject_grants: Sequence[Any],
    legacy_group_ids: set[str],
    enabled_group_ids: set[str],
    member_group_ids: set[str],
    erp_ctx: Any | None,
) -> bool:
    if is_admin or is_owner:
        return True
    if policy is None:
        if not legacy_group_ids:
            return True
        active = legacy_group_ids & enabled_group_ids
        if not active:
            return False
        return bool(active & member_group_ids)

    mode = str(getattr(policy, "visibility_mode", "") or "")
    policy_company_id = int(getattr(policy, "company_id", 0) or 0)
    if mode == VISIBILITY_OWNER:
        return False
    if policy_company_id > 0:
        if erp_ctx is None or not getattr(erp_ctx, "active_in_erp", False):
            return False
        if int(getattr(erp_ctx, "company_id", 0) or 0) != policy_company_id:
            return False

    def _matches(grant: Any) -> bool:
        st = str(getattr(grant, "subject_type", "") or "")
        sid = str(getattr(grant, "subject_id", "") or "")
        if st == SUBJECT_USER:
            return sid == str(user_id)
        if st == SUBJECT_USER_GROUP:
            return sid in enabled_group_ids and sid in member_group_ids
        if st == SUBJECT_ERP_EMPLOYEE:
            if erp_ctx is None or not getattr(erp_ctx, "active_in_erp", False):
                return False
            return sid == erp_employee_subject_id(
                int(erp_ctx.company_id), int(erp_ctx.employee_id)
            )
        if st == SUBJECT_DEPARTMENT:
            if erp_ctx is None or not getattr(erp_ctx, "active_in_erp", False):
                return False
            parsed = parse_scoped_pair(sid)
            if parsed is None:
                return False
            g_cid, g_org_id = parsed
            if g_cid != int(erp_ctx.company_id):
                return False
            pool = (
                erp_ctx.ancestor_org_ids
                if bool(getattr(grant, "include_children", True))
                else erp_ctx.direct_org_ids
            )
            return g_org_id in pool
        return False

    denies = [g for g in subject_grants if str(getattr(g, "effect", "allow")) == "deny"]
    if any(_matches(g) for g in denies):
        return False

    if mode == VISIBILITY_ALL:
        return True
    if mode == VISIBILITY_COMPANY:
        if erp_ctx is None or not getattr(erp_ctx, "active_in_erp", False):
            return False
        return policy_company_id == 0 or int(erp_ctx.company_id) == policy_company_id
    if mode in {VISIBILITY_SCOPED, VISIBILITY_GROUPS}:
        allows = [g for g in subject_grants if str(getattr(g, "effect", "allow")) == "allow"]
        if any(_matches(g) for g in allows):
            return True
        if legacy_group_ids:
            active = legacy_group_ids & enabled_group_ids
            if active and (active & member_group_ids):
                return True
        return False
    return False


def _extract_grant_field(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _normalize_dept_grants(
    raw_items: Sequence[Any] | None, *, default_company_id: int
) -> tuple[dict[str, Any], ...]:
    out: list[dict[str, Any]] = []
    by_key: dict[tuple[int, int], dict[str, Any]] = {}
    for item in raw_items or []:
        cid_raw = _extract_grant_field(item, "company_id")
        oid_raw = _extract_grant_field(item, "org_id")
        if oid_raw is None:
            continue
        cid = int(cid_raw) if cid_raw is not None else int(default_company_id or 0)
        oid = int(oid_raw)
        if cid <= 0 or oid <= 0:
            raise OctopError(ErrorCode.FORBIDDEN, "invalid department scope target", status=400)
        inc_raw = _extract_grant_field(item, "include_children", True)
        include_children = True if inc_raw is None else bool(inc_raw)
        key = (cid, oid)
        if key in by_key:
            # Merge conservatively: including children widens over self-only.
            by_key[key]["include_children"] = bool(
                by_key[key]["include_children"] or include_children
            )
            continue
        entry = {
            "company_id": cid,
            "org_id": oid,
            "include_children": include_children,
        }
        by_key[key] = entry
        out.append(entry)
    return tuple(out)


def _normalize_emp_grants(
    raw_items: Sequence[Any] | None, *, default_company_id: int
) -> tuple[dict[str, Any], ...]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for item in raw_items or []:
        cid_raw = _extract_grant_field(item, "company_id")
        emp_raw = _extract_grant_field(item, "employee_id")
        if emp_raw is None:
            continue
        cid = int(cid_raw) if cid_raw is not None else int(default_company_id or 0)
        emp_id = int(emp_raw)
        if cid <= 0 or emp_id <= 0:
            raise OctopError(ErrorCode.FORBIDDEN, "invalid ERP employee target", status=400)
        key = (cid, emp_id)
        if key in seen:
            continue
        seen.add(key)
        out.append({"company_id": cid, "employee_id": emp_id})
    return tuple(out)


def _normalize_user_ids(raw_ids: Sequence[Any] | None) -> tuple[int, ...]:
    out: list[int] = []
    seen: set[int] = set()
    for raw in raw_ids or []:
        try:
            uid = int(raw)
        except (TypeError, ValueError) as exc:
            raise OctopError(ErrorCode.FORBIDDEN, "invalid user id", status=400) from exc
        if uid <= 0:
            raise OctopError(ErrorCode.FORBIDDEN, "invalid user id", status=400)
        if uid not in seen:
            seen.add(uid)
            out.append(uid)
    return tuple(out)


class GroupAccess:
    """Read and write unified visibility scopes. Callers still apply ownership themselves."""

    def __init__(self, repos: Any) -> None:
        self._repos = repos
        self._groups = getattr(repos, "user_group_repo", None)
        self._members = getattr(repos, "user_group_member_repo", None)
        self._grants = getattr(repos, "resource_group_grant_repo", None)
        self._policies_grants = getattr(repos, "resource_grant_repo", None)
        self._erp_org = getattr(repos, "erp_org_repo", None)
        self._users = getattr(repos, "user_repo", None)

    def _get_erp_ctx(self, user_id: int) -> Any | None:
        if self._erp_org is None:
            return None
        return self._erp_org.get_user_erp_context(user_id)

    def _get_policy_and_grants(
        self, resource_type: str, resource_key: str
    ) -> tuple[Any | None, list[Any]]:
        if self._policies_grants is None:
            return None, []
        return (
            self._policies_grants.get_policy(resource_type, resource_key),
            self._policies_grants.list_grants(resource_type, resource_key),
        )

    def allows(
        self,
        *,
        user_id: int,
        is_admin: bool,
        resource_type: str,
        resource_key: str,
        is_owner: bool,
    ) -> bool:
        if is_admin or is_owner:
            return True
        policy, subject_grants = self._get_policy_and_grants(resource_type, resource_key)
        legacy_groups = (
            self._grants.group_ids(resource_type, resource_key)
            if self._grants is not None
            else set()
        )
        enabled = self._enabled_group_ids()
        member_groups = (
            self._members.group_ids_for_user(user_id) if self._members is not None else set()
        )
        erp_ctx = self._get_erp_ctx(user_id) if policy is not None else None
        return _evaluate_access(
            user_id=user_id,
            is_admin=is_admin,
            is_owner=is_owner,
            policy=policy,
            subject_grants=subject_grants,
            legacy_group_ids=legacy_groups,
            enabled_group_ids=enabled,
            member_group_ids=member_groups,
            erp_ctx=erp_ctx,
        )

    def assert_allowed(
        self,
        *,
        user_id: int,
        is_admin: bool,
        resource_type: str,
        resource_key: str,
        is_owner: bool,
    ) -> None:
        if self.allows(
            user_id=user_id,
            is_admin=is_admin,
            resource_type=resource_type,
            resource_key=resource_key,
            is_owner=is_owner,
        ):
            return
        raise OctopError(ErrorCode.RESOURCE_NOT_ALLOWED, "resource is not visible to this user")

    def filter_rows(
        self,
        rows: Sequence[T],
        *,
        user_id: int,
        is_admin: bool,
        resource_type: str,
        key_of: Callable[[T], str],
        is_owner: Callable[[T], bool],
    ) -> list[T]:
        if is_admin:
            return list(rows)
        policies_by_key = (
            self._policies_grants.list_policies_by_type(resource_type)
            if self._policies_grants is not None
            else {}
        )
        grants_by_key = (
            self._policies_grants.list_grants_by_type(resource_type)
            if self._policies_grants is not None
            else {}
        )
        legacy_by_key = (
            self._grants.group_ids_by_resource(resource_type)
            if self._grants is not None
            else {}
        )
        if not policies_by_key and not grants_by_key and not legacy_by_key:
            return list(rows)
        enabled = self._enabled_group_ids()
        member_groups = (
            self._members.group_ids_for_user(user_id) if self._members is not None else set()
        )
        erp_ctx = self._get_erp_ctx(user_id) if policies_by_key else None
        kept: list[T] = []
        for row in rows:
            if is_owner(row):
                kept.append(row)
                continue
            key = key_of(row)
            if _evaluate_access(
                user_id=user_id,
                is_admin=False,
                is_owner=False,
                policy=policies_by_key.get(key),
                subject_grants=grants_by_key.get(key, ()),
                legacy_group_ids=legacy_by_key.get(key, set()),
                enabled_group_ids=enabled,
                member_group_ids=member_groups,
                erp_ctx=erp_ctx,
            ):
                kept.append(row)
        return kept

    @classmethod
    def filter_rows_with_db(
        cls,
        db: Any,
        rows: Sequence[T],
        *,
        user_id: int,
        is_admin: bool,
        resource_type: str,
        key_of: Callable[[T], str],
        is_owner: Callable[[T], bool],
    ) -> list[T]:
        if is_admin or not rows:
            return list(rows)
        from octop.infra.db.services import RepoBundle  # noqa: PLC0415

        return cls(RepoBundle.from_pool(db)).filter_rows(
            rows,
            user_id=user_id,
            is_admin=is_admin,
            resource_type=resource_type,
            key_of=key_of,
            is_owner=is_owner,
        )

    def _enabled_group_ids(self) -> set[str]:
        if self._groups is None:
            return set()
        return {row.user_group_id for row in self._groups.list_all() if row.enabled}

    def visibility(
        self,
        resource_type: str,
        resource_key: str,
        *,
        shared: bool | None,
        preloaded_group_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Legacy visibility view (``visibility`` + ``group_ids`` only)."""
        if shared is False:
            return {"visibility": VISIBILITY_OWNER, "group_ids": []}
        policy, subject_grants = self._get_policy_and_grants(resource_type, resource_key)
        raw_legacy = (
            preloaded_group_ids
            if preloaded_group_ids is not None
            else (
                self._grants.group_ids(resource_type, resource_key)
                if self._grants is not None
                else set()
            )
        )
        legacy_groups = sorted(raw_legacy)
        if policy is not None:
            group_ids = sorted(
                {
                    str(g.subject_id)
                    for g in subject_grants
                    if g.subject_type == SUBJECT_USER_GROUP and g.effect == "allow"
                }
                | set(legacy_groups)
            )
            return {"visibility": policy.visibility_mode, "group_ids": group_ids}
        if not legacy_groups:
            return {"visibility": VISIBILITY_ALL, "group_ids": []}
        return {"visibility": VISIBILITY_GROUPS, "group_ids": legacy_groups}

    def scope_payload(
        self,
        resource_type: str,
        resource_key: str,
        *,
        shared: bool | None,
        preloaded_group_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        """Full resource scope payload for Web APIs (backward compatible with visibility)."""
        base: dict[str, Any] = {
            "visibility": VISIBILITY_OWNER if shared is False else VISIBILITY_ALL,
            "group_ids": [],
            "company_id": 0,
            "department_grants": [],
            "user_ids": [],
            "erp_employees": [],
            "deny_department_grants": [],
            "deny_user_ids": [],
            "deny_erp_employees": [],
        }
        if shared is False:
            return base
        policy, subject_grants = self._get_policy_and_grants(resource_type, resource_key)
        raw_legacy = (
            preloaded_group_ids
            if preloaded_group_ids is not None
            else (
                self._grants.group_ids(resource_type, resource_key)
                if self._grants is not None
                else set()
            )
        )
        legacy_groups = sorted(raw_legacy)
        if policy is None:
            if legacy_groups:
                base["visibility"] = VISIBILITY_GROUPS
                base["group_ids"] = legacy_groups
            return base

        base["visibility"] = policy.visibility_mode
        base["company_id"] = int(policy.company_id or 0)
        allow_groups: set[str] = set(legacy_groups)
        dept_grants: list[dict[str, Any]] = []
        user_ids: list[int] = []
        erp_emps: list[dict[str, Any]] = []
        deny_depts: list[dict[str, Any]] = []
        deny_users: list[int] = []
        deny_emps: list[dict[str, Any]] = []

        for g in subject_grants:
            if g.subject_type == SUBJECT_USER_GROUP and g.effect == "allow":
                allow_groups.add(g.subject_id)
            elif g.subject_type == SUBJECT_DEPARTMENT:
                parsed = parse_scoped_pair(g.subject_id)
                if parsed is not None:
                    item = {
                        "company_id": parsed[0],
                        "org_id": parsed[1],
                        "include_children": bool(g.include_children),
                    }
                    (deny_depts if g.effect == "deny" else dept_grants).append(item)
            elif g.subject_type == SUBJECT_USER and g.subject_id.isdigit():
                (deny_users if g.effect == "deny" else user_ids).append(int(g.subject_id))
            elif g.subject_type == SUBJECT_ERP_EMPLOYEE:
                parsed = parse_scoped_pair(g.subject_id)
                if parsed is not None:
                    item = {"company_id": parsed[0], "employee_id": parsed[1]}
                    (deny_emps if g.effect == "deny" else erp_emps).append(item)

        base["group_ids"] = sorted(allow_groups)
        base["department_grants"] = dept_grants
        base["user_ids"] = sorted(set(user_ids))
        base["erp_employees"] = erp_emps
        base["deny_department_grants"] = deny_depts
        base["deny_user_ids"] = sorted(set(deny_users))
        base["deny_erp_employees"] = deny_emps
        return base

    def validate_visibility(
        self,
        visibility: str,
        group_ids: Sequence[str] | None,
    ) -> list[str]:
        """Validate legacy visibility args before any ``shared`` write."""
        if visibility in {VISIBILITY_ALL, VISIBILITY_OWNER}:
            return []
        if visibility != VISIBILITY_GROUPS:
            raise OctopError(ErrorCode.FORBIDDEN, "unknown visibility", status=400)
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in group_ids or []:
            group_id = str(raw).strip()
            if not group_id or group_id in seen:
                continue
            seen.add(group_id)
            cleaned.append(group_id)
        if not cleaned:
            raise OctopError(
                ErrorCode.FORBIDDEN,
                "choose at least one user group",
                status=400,
            )
        if self._groups is not None:
            missing = [group_id for group_id in cleaned if self._groups.get(group_id) is None]
            if missing:
                raise OctopError(ErrorCode.NOT_FOUND, "user group not found")
        return cleaned

    def validate_scope(
        self,
        *,
        visibility: str,
        group_ids: Sequence[str] | None = None,
        company_id: int | None = None,
        department_grants: Sequence[Any] | None = None,
        user_ids: Sequence[Any] | None = None,
        erp_employees: Sequence[Any] | None = None,
        deny_department_grants: Sequence[Any] | None = None,
        deny_user_ids: Sequence[Any] | None = None,
        deny_erp_employees: Sequence[Any] | None = None,
        actor_user_id: int | None = None,
        actor_is_admin: bool = False,
    ) -> NormalizedScope:
        if visibility not in VALID_VISIBILITIES:
            raise OctopError(ErrorCode.FORBIDDEN, "unknown visibility", status=400)

        raw_cid = int(company_id or 0)
        uses_extended = (
            visibility in {VISIBILITY_COMPANY, VISIBILITY_SCOPED}
            or raw_cid > 0
            or bool(
                department_grants
                or user_ids
                or erp_employees
                or deny_department_grants
                or deny_user_ids
                or deny_erp_employees
            )
        )

        if visibility == VISIBILITY_OWNER:
            return NormalizedScope(
                visibility=VISIBILITY_OWNER,
                company_id=0,
                group_ids=(),
                department_grants=(),
                user_ids=(),
                erp_employees=(),
                deny_department_grants=(),
                deny_user_ids=(),
                deny_erp_employees=(),
            )

        if not uses_extended:
            cleaned_groups = self.validate_visibility(visibility, group_ids)
            return NormalizedScope(
                visibility=visibility,
                company_id=0,
                group_ids=tuple(cleaned_groups),
                department_grants=(),
                user_ids=(),
                erp_employees=(),
                deny_department_grants=(),
                deny_user_ids=(),
                deny_erp_employees=(),
            )

        resolved_cid = raw_cid
        if uses_extended and not actor_is_admin and actor_user_id is not None:
            actor_ctx = self._get_erp_ctx(actor_user_id)
            if actor_ctx is not None and actor_ctx.company_id > 0:
                actor_cid = int(actor_ctx.company_id)
                if raw_cid > 0 and raw_cid != actor_cid:
                    raise OctopError(
                        ErrorCode.FORBIDDEN,
                        "cross-company scope target is not allowed",
                        status=403,
                    )
                resolved_cid = actor_cid

        if visibility == VISIBILITY_COMPANY and resolved_cid <= 0:
            if actor_user_id is not None:
                actor_ctx = self._get_erp_ctx(actor_user_id)
                if actor_ctx is not None and actor_ctx.company_id > 0:
                    resolved_cid = int(actor_ctx.company_id)
            if resolved_cid <= 0 and self._erp_org is not None:
                known = self._erp_org.known_company_ids()
                if len(known) == 1:
                    resolved_cid = int(known[0])

        cleaned_groups_list: list[str] = []
        seen_groups: set[str] = set()
        for raw in group_ids or []:
            gid = str(raw).strip()
            if gid and gid not in seen_groups:
                seen_groups.add(gid)
                cleaned_groups_list.append(gid)
        if self._groups is not None and cleaned_groups_list:
            missing = [gid for gid in cleaned_groups_list if self._groups.get(gid) is None]
            if missing:
                raise OctopError(ErrorCode.NOT_FOUND, "user group not found")

        norm_depts = _normalize_dept_grants(department_grants, default_company_id=resolved_cid)
        norm_users = _normalize_user_ids(user_ids)
        norm_emps = _normalize_emp_grants(erp_employees, default_company_id=resolved_cid)
        norm_deny_depts = _normalize_dept_grants(
            deny_department_grants, default_company_id=resolved_cid
        )
        norm_deny_users = _normalize_user_ids(deny_user_ids)
        norm_deny_emps = _normalize_emp_grants(
            deny_erp_employees, default_company_id=resolved_cid
        )

        if resolved_cid > 0:
            for item in (*norm_depts, *norm_deny_depts, *norm_emps, *norm_deny_emps):
                if int(item["company_id"]) != resolved_cid:
                    raise OctopError(
                        ErrorCode.FORBIDDEN,
                        "cross-company scope target is not allowed",
                        status=400,
                    )

        if self._erp_org is not None:
            for item in (*norm_depts, *norm_deny_depts):
                dept = self._erp_org.get_department(item["company_id"], item["org_id"])
                if dept is None or dept.status != 1:
                    raise OctopError(ErrorCode.NOT_FOUND, "ERP department not found")
            for item in (*norm_emps, *norm_deny_emps):
                emp = self._erp_org.get_employee(item["company_id"], item["employee_id"])
                if emp is None or emp.status != 1:
                    raise OctopError(ErrorCode.NOT_FOUND, "ERP employee not found")

        if self._users is not None:
            for uid in (*norm_users, *norm_deny_users):
                row = self._users.get(uid)
                if row is None or getattr(row, "disabled", 0):
                    raise OctopError(ErrorCode.NOT_FOUND, "user not found")

        if visibility == VISIBILITY_GROUPS:
            if not cleaned_groups_list:
                raise OctopError(ErrorCode.FORBIDDEN, "choose at least one user group", status=400)
            return NormalizedScope(
                visibility=VISIBILITY_GROUPS,
                company_id=resolved_cid,
                group_ids=tuple(cleaned_groups_list),
                department_grants=(),
                user_ids=(),
                erp_employees=(),
                deny_department_grants=norm_deny_depts,
                deny_user_ids=norm_deny_users,
                deny_erp_employees=norm_deny_emps,
            )

        if visibility == VISIBILITY_SCOPED and not (
            cleaned_groups_list or norm_depts or norm_users or norm_emps
        ):
            raise OctopError(
                ErrorCode.FORBIDDEN,
                "choose at least one department, user, or user group",
                status=400,
            )

        if visibility in {VISIBILITY_ALL, VISIBILITY_COMPANY}:
            # Top-level broad visibility keeps optional deny overrides only.
            cleaned_groups_list = []
            norm_depts = ()
            norm_users = ()
            norm_emps = ()

        return NormalizedScope(
            visibility=visibility,
            company_id=resolved_cid,
            group_ids=tuple(cleaned_groups_list),
            department_grants=norm_depts,
            user_ids=norm_users,
            erp_employees=norm_emps,
            deny_department_grants=norm_deny_depts,
            deny_user_ids=norm_deny_users,
            deny_erp_employees=norm_deny_emps,
        )

    def set_visibility(
        self,
        resource_type: str,
        resource_key: str,
        visibility: str,
        group_ids: Sequence[str] | None,
    ) -> None:
        if resource_type not in RESOURCE_TYPES:
            raise OctopError(ErrorCode.FORBIDDEN, "unknown resource type", status=400)
        cleaned = self.validate_visibility(visibility, group_ids)
        if visibility in {VISIBILITY_ALL, VISIBILITY_OWNER}:
            if self._grants is not None:
                self._grants.delete_resource(resource_type, resource_key)
            if self._policies_grants is not None:
                self._policies_grants.delete_resource(resource_type, resource_key)
            return
        if self._grants is not None:
            self._grants.replace(resource_type, resource_key, cleaned)
        if self._policies_grants is not None:
            self._policies_grants.delete_resource(resource_type, resource_key)

    def set_scope(
        self,
        resource_type: str,
        resource_key: str,
        scope: NormalizedScope,
        *,
        actor_user_id: int | None = None,
    ) -> None:
        if resource_type not in RESOURCE_TYPES:
            raise OctopError(ErrorCode.FORBIDDEN, "unknown resource type", status=400)
        grant_rows = scope.to_grant_rows()
        if (
            scope.visibility in {VISIBILITY_OWNER, VISIBILITY_ALL, VISIBILITY_GROUPS}
            and scope.company_id == 0
            and not any(row["effect"] == "deny" for row in grant_rows)
            and not scope.department_grants
            and not scope.user_ids
            and not scope.erp_employees
        ):
            self.set_visibility(
                resource_type,
                resource_key,
                scope.visibility,
                list(scope.group_ids),
            )
            return
        if self._policies_grants is None:
            raise OctopError(ErrorCode.INTERNAL_ERROR, "resource_grant_repo unavailable")
        self._policies_grants.replace_grants(
            resource_type,
            resource_key,
            company_id=scope.company_id,
            visibility_mode=scope.visibility,
            grants=grant_rows,
            updated_by=actor_user_id,
            sync_legacy_group_grants=True,
        )

    def clear(self, resource_type: str, resource_key: str) -> None:
        if self._grants is not None:
            self._grants.delete_resource(resource_type, resource_key)
        if self._policies_grants is not None:
            self._policies_grants.delete_resource(resource_type, resource_key)

    def check_agent_kb_gaps(
        self,
        *,
        kb_ids: Sequence[str],
        scope: NormalizedScope,
        actor_user_id: int | None = None,
        actor_is_admin: bool = True,
    ) -> list[dict[str, Any]]:
        """Check which non-private knowledge bases bound to an agent lack coverage for *scope*."""
        kb_repo = getattr(self._repos, "knowledge_repo", None)
        if kb_repo is None or not kb_ids:
            return []
        if scope.visibility == VISIBILITY_OWNER:
            return []

        allow_rows = scope.to_allow_grant_rows()
        if scope.visibility == VISIBILITY_SCOPED and not allow_rows:
            return []

        gaps: list[dict[str, Any]] = []
        for raw_kb_id in kb_ids:
            kb_id = str(raw_kb_id or "").strip()
            if not kb_id or kb_id.startswith("private-feishu:"):
                continue
            kb = kb_repo.get_base(kb_id)
            if kb is None:
                continue
            if not actor_is_admin and int(getattr(kb, "owner_user_id", 0) or 0) != int(
                actor_user_id or 0
            ):
                continue
            current = self.scope_payload(
                RESOURCE_KNOWLEDGE_BASE,
                kb.id,
                shared=bool(kb.shared),
            )
            cur_vis = current["visibility"]
            if cur_vis == VISIBILITY_ALL:
                continue
            if cur_vis == VISIBILITY_COMPANY:
                cur_cid = int(current.get("company_id") or 0)
                if cur_cid == 0 or (scope.company_id > 0 and cur_cid == scope.company_id):
                    continue
            if cur_vis == VISIBILITY_OWNER:
                gaps.append(
                    {
                        "kb_id": kb.id,
                        "name": kb.name,
                        "visibility": cur_vis,
                        "reason": "private_owner",
                    }
                )
                continue

            if scope.visibility in {VISIBILITY_ALL, VISIBILITY_COMPANY}:
                gaps.append(
                    {
                        "kb_id": kb.id,
                        "name": kb.name,
                        "visibility": cur_vis,
                        "reason": "narrower_than_agent",
                    }
                )
                continue

            # Compare scoped/groups targets.
            cur_groups = set(current.get("group_ids") or [])
            cur_users = set(current.get("user_ids") or [])
            cur_emps = {
                (int(e["company_id"]), int(e["employee_id"]))
                for e in (current.get("erp_employees") or [])
            }
            cur_depts = {
                (int(d["company_id"]), int(d["org_id"])): bool(d.get("include_children", True))
                for d in (current.get("department_grants") or [])
            }
            missing = False
            for gid in scope.group_ids:
                if gid not in cur_groups:
                    missing = True
                    break
            if not missing:
                for uid in scope.user_ids:
                    if uid not in cur_users:
                        missing = True
                        break
            if not missing:
                for emp in scope.erp_employees:
                    if (int(emp["company_id"]), int(emp["employee_id"])) not in cur_emps:
                        missing = True
                        break
            if not missing:
                for dept in scope.department_grants:
                    key = (int(dept["company_id"]), int(dept["org_id"]))
                    if key not in cur_depts:
                        missing = True
                        break
                    if dept.get("include_children", True) and not cur_depts[key]:
                        missing = True
                        break
            if missing:
                gaps.append(
                    {
                        "kb_id": kb.id,
                        "name": kb.name,
                        "visibility": cur_vis,
                        "reason": "missing_targets",
                    }
                )
        return gaps

    def sync_agent_kb_grants(
        self,
        *,
        kb_ids: Sequence[str],
        scope: NormalizedScope,
        actor_user_id: int | None,
        actor_is_admin: bool = True,
    ) -> list[str]:
        """Grant target scope read access on linked non-private KBs when confirmed."""
        kb_repo = getattr(self._repos, "knowledge_repo", None)
        if kb_repo is None or self._policies_grants is None or not kb_ids:
            return []
        gaps = self.check_agent_kb_gaps(
            kb_ids=kb_ids,
            scope=scope,
            actor_user_id=actor_user_id,
            actor_is_admin=actor_is_admin,
        )
        if not gaps:
            return []
        updated_ids: list[str] = []
        allow_rows = scope.to_allow_grant_rows()
        for gap in gaps:
            kb_id = gap["kb_id"]
            kb = kb_repo.get_base(kb_id)
            if kb is None:
                continue
            if not kb.shared:
                kb_repo.update_base(kb.id, shared=True)
            if scope.visibility == VISIBILITY_ALL:
                self.set_scope(
                    RESOURCE_KNOWLEDGE_BASE,
                    kb.id,
                    NormalizedScope(
                        visibility=VISIBILITY_ALL,
                        company_id=0,
                        group_ids=(),
                        department_grants=(),
                        user_ids=(),
                        erp_employees=(),
                        deny_department_grants=(),
                        deny_user_ids=(),
                        deny_erp_employees=(),
                    ),
                    actor_user_id=actor_user_id,
                )
            elif scope.visibility == VISIBILITY_COMPANY:
                self.set_scope(
                    RESOURCE_KNOWLEDGE_BASE,
                    kb.id,
                    NormalizedScope(
                        visibility=VISIBILITY_COMPANY,
                        company_id=scope.company_id,
                        group_ids=(),
                        department_grants=(),
                        user_ids=(),
                        erp_employees=(),
                        deny_department_grants=(),
                        deny_user_ids=(),
                        deny_erp_employees=(),
                    ),
                    actor_user_id=actor_user_id,
                )
            elif allow_rows:
                self._policies_grants.append_allow_grants(
                    RESOURCE_KNOWLEDGE_BASE,
                    kb.id,
                    company_id=scope.company_id,
                    grants=allow_rows,
                    granted_by=actor_user_id,
                )
            updated_ids.append(kb.id)
        return updated_ids


def expert_visible_to(
    repos: Any,
    row: Any,
    *,
    user_id: int,
    is_admin: bool,
) -> bool:
    """Owner and admin always pass. Others need ``is_shared`` plus scope checks."""
    if is_admin:
        return True
    owner_id = getattr(row, "user_id", None)
    if owner_id is not None and int(owner_id) == int(user_id):
        return True
    if int(getattr(row, "is_shared", 0) or 0) != 1:
        return False
    return GroupAccess(repos).allows(
        user_id=user_id,
        is_admin=False,
        resource_type=RESOURCE_EXPERT,
        resource_key=str(getattr(row, "agent_id", "")),
        is_owner=False,
    )


def empty_group_access() -> dict[str, Any]:
    return {
        "connectors": [],
        "storage": [],
        "models": [],
        "plugins": [],
        "skill_packages": [],
        "knowledge_bases": [],
        "bridges": [],
        "experts": [],
    }


def _access_item(key: str, label: str, *, audience: str, missing: bool = False) -> dict[str, Any]:
    return {"key": key, "label": label, "missing": missing, "audience": audience}


def _model_catalog(repos: Any) -> tuple[dict[str, str], set[str]]:
    """Return ``(labels, usable keys)``. Disabled providers and models are not usable."""
    labels: dict[str, str] = {}
    usable: set[str] = set()
    for provider in repos.provider_repo.list_all():
        provider_on = bool(provider.enabled)
        for model in provider.get_models():
            if not isinstance(model, dict):
                continue
            model_id = str(model.get("id") or "").strip()
            if not model_id:
                continue
            name = str(model.get("name") or model_id).strip() or model_id
            key = model_resource_key(provider.name, model_id)
            labels[key] = f"{name} · {provider.name}"
            if provider_on and model.get("enabled") is not False:
                usable.add(key)
    return labels, usable


_AUDIENCE_ORDER = {"groups": 0, "all": 1, "creator": 2, "owner": 3}


def _resource_items(
    *,
    catalog: dict[str, str],
    private: dict[str, str],
    private_audience: str,
    known: set[str],
    grants: dict[str, set[str]],
    group_id: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key, label in catalog.items():
        granted = grants.get(key, set())
        if not granted:
            audience = "all"
        elif group_id in granted:
            audience = "groups"
        else:
            continue
        seen.add(key)
        items.append(_access_item(key, label, audience=audience))
    for key, label in private.items():
        if key in seen:
            continue
        seen.add(key)
        items.append(_access_item(key, label, audience=private_audience))
    for key, granted in grants.items():
        if key in seen or key in known or group_id not in granted:
            continue
        items.append(_access_item(key, key, audience="groups", missing=True))
    items.sort(
        key=lambda item: (
            _AUDIENCE_ORDER.get(str(item["audience"]), 9),
            str(item["label"]).lower(),
        )
    )
    return items


def group_access_by_group(repos: Any) -> dict[str, dict[str, Any]]:
    """What each group can use."""
    model_labels, usable_models = _model_catalog(repos)
    package_rows = repos.skill_package_repo.list_all()
    base_rows = repos.knowledge_repo.list_all()
    packages = {row.id: row.name for row in package_rows if row.shared}
    private_packages = {row.id: row.name for row in package_rows if not row.shared}
    bases = {row.id: row.name for row in base_rows if row.shared}
    private_bases = {row.id: row.name for row in base_rows if not row.shared}
    grants: dict[tuple[str, str], set[str]] = {}
    for group_id, resource_type, resource_key in repos.resource_group_grant_repo.list_all():
        grants.setdefault((resource_type, resource_key), set()).add(group_id)

    def keys(resource_type: str) -> dict[str, set[str]]:
        return {
            key: group_ids for (kind, key), group_ids in grants.items() if kind == resource_type
        }

    model_grants = keys(RESOURCE_MODEL)
    package_grants = keys(RESOURCE_SKILL_PACKAGE)
    base_grants = keys(RESOURCE_KNOWLEDGE_BASE)
    connector_grants = keys(RESOURCE_CONNECTOR)
    storage_grants = keys(RESOURCE_STORAGE_BACKEND)
    plugin_grants = keys(RESOURCE_PLUGIN)
    bridge_grants = keys(RESOURCE_BRIDGE)
    expert_grants = keys(RESOURCE_EXPERT)
    agent_rows = list(repos.agent_repo.list_all())
    shared_experts = {
        row.agent_id: row.name for row in agent_rows if int(getattr(row, "is_shared", 0) or 0) == 1
    }
    private_experts = {
        row.agent_id: row.name for row in agent_rows if int(getattr(row, "is_shared", 0) or 0) != 1
    }
    bridge_rows = list(repos.bridge_connection_repo.list_all())
    shared_bridges = {row.connection_id: row.display_name for row in bridge_rows if row.shared}
    private_bridges = {row.connection_id: row.display_name for row in bridge_rows if not row.shared}
    connector_rows = [
        row
        for row in repos.connector_repo.list_all()
        if str(getattr(row, "kind", "") or "") != "custom-mcp"
    ]
    shared_connectors = {row.instance_id: row.display_name for row in connector_rows if row.shared}
    private_connectors = {
        row.instance_id: row.display_name for row in connector_rows if not row.shared
    }
    connector_grants.pop(CONNECTOR_FEATURE_KEY, None)
    storage_grants.pop(STORAGE_FEATURE_KEY, None)
    storage_rows = list(repos.storage_backend_repo.list_all())
    storage_catalog = {row.name: row.name for row in storage_rows}
    plugin_catalog = {key: key for key in plugin_grants}
    out: dict[str, dict[str, Any]] = {}
    for group in repos.user_group_repo.list_all():
        group_id = group.user_group_id
        summary = empty_group_access()
        summary["connectors"] = _resource_items(
            catalog=shared_connectors,
            private=private_connectors,
            private_audience="owner",
            known={row.instance_id for row in connector_rows},
            grants=connector_grants,
            group_id=group_id,
        )
        summary["storage"] = _resource_items(
            catalog=storage_catalog,
            private={},
            private_audience="owner",
            known=set(storage_catalog),
            grants=storage_grants,
            group_id=group_id,
        )
        summary["models"] = _resource_items(
            catalog={key: model_labels[key] for key in usable_models},
            private={},
            private_audience="owner",
            known=set(model_labels),
            grants=model_grants,
            group_id=group_id,
        )
        summary["plugins"] = _resource_items(
            catalog=plugin_catalog,
            private={},
            private_audience="owner",
            known=set(plugin_catalog),
            grants=plugin_grants,
            group_id=group_id,
        )
        summary["skill_packages"] = _resource_items(
            catalog=packages,
            private=private_packages,
            private_audience="creator",
            known={row.id for row in package_rows},
            grants=package_grants,
            group_id=group_id,
        )
        summary["knowledge_bases"] = _resource_items(
            catalog=bases,
            private=private_bases,
            private_audience="owner",
            known={row.id for row in base_rows},
            grants=base_grants,
            group_id=group_id,
        )
        summary["bridges"] = _resource_items(
            catalog=shared_bridges,
            private=private_bridges,
            private_audience="owner",
            known={row.connection_id for row in bridge_rows},
            grants=bridge_grants,
            group_id=group_id,
        )
        summary["experts"] = _resource_items(
            catalog=shared_experts,
            private=private_experts,
            private_audience="owner",
            known={row.agent_id for row in agent_rows},
            grants=expert_grants,
            group_id=group_id,
        )
        out[group_id] = summary
    return out
