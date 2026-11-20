"""Shared Pydantic scope fields and helpers for Web resource routers."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from octop.infra.users.groups import (
    VISIBILITY_ALL,
    VISIBILITY_COMPANY,
    VISIBILITY_GROUPS,
    VISIBILITY_OWNER,
    VISIBILITY_SCOPED,
    GroupAccess,
    NormalizedScope,
    scope_fields_from_body,
)


class DepartmentGrantInput(BaseModel):
    company_id: int | None = None
    org_id: int
    include_children: bool = True


class ErpEmployeeGrantInput(BaseModel):
    company_id: int | None = None
    employee_id: int


class ResourceScopeFields(BaseModel):
    visibility: Literal["owner", "all", "groups", "company", "scoped"] | None = None
    group_ids: list[str] | None = None
    company_id: int | None = None
    department_grants: list[DepartmentGrantInput] | None = None
    user_ids: list[int] | None = None
    erp_employees: list[ErpEmployeeGrantInput] | None = None
    deny_department_grants: list[DepartmentGrantInput] | None = None
    deny_user_ids: list[int] | None = None
    deny_erp_employees: list[ErpEmployeeGrantInput] | None = None


SCOPE_FIELD_NAMES = frozenset(
    {
        "visibility",
        "group_ids",
        "company_id",
        "department_grants",
        "user_ids",
        "erp_employees",
        "deny_department_grants",
        "deny_user_ids",
        "deny_erp_employees",
    }
)


def scope_shared_flag(visibility: str | None, fallback_shared: bool | None) -> bool | None:
    if visibility == VISIBILITY_OWNER:
        return False
    if visibility in {VISIBILITY_ALL, VISIBILITY_GROUPS, VISIBILITY_COMPANY, VISIBILITY_SCOPED}:
        return True
    return fallback_shared


def validate_body_scope(
    access: GroupAccess | None,
    body: Any,
    *,
    user: Any,
    fallback_visibility: str | None = None,
) -> tuple[dict[str, Any] | None, NormalizedScope | None]:
    """Validate extended or legacy visibility args before mutating the target row."""
    if access is None:
        return None, None
    scope_dict = scope_fields_from_body(body)
    visibility = getattr(body, "visibility", None)
    if scope_dict is not None:
        eff_vis = visibility or fallback_visibility or VISIBILITY_OWNER
        normalized = access.validate_scope(
            visibility=eff_vis,
            group_ids=scope_dict.get("group_ids"),
            company_id=scope_dict.get("company_id"),
            department_grants=scope_dict.get("department_grants"),
            user_ids=scope_dict.get("user_ids"),
            erp_employees=scope_dict.get("erp_employees"),
            deny_department_grants=scope_dict.get("deny_department_grants"),
            deny_user_ids=scope_dict.get("deny_user_ids"),
            deny_erp_employees=scope_dict.get("deny_erp_employees"),
            actor_user_id=user.id,
            actor_is_admin=bool(getattr(user, "is_admin", False)),
        )
        return scope_dict, normalized
    if visibility is not None:
        access.validate_visibility(visibility, getattr(body, "group_ids", None))
    return None, None


def apply_body_scope(
    access: GroupAccess | None,
    *,
    resource_type: str,
    resource_key: str,
    body: Any,
    normalized: NormalizedScope | None,
    user: Any,
) -> None:
    if access is None:
        return
    if normalized is not None:
        access.set_scope(
            resource_type,
            resource_key,
            normalized,
            actor_user_id=user.id,
        )
        return
    visibility = getattr(body, "visibility", None)
    if visibility is not None:
        access.set_visibility(
            resource_type,
            resource_key,
            visibility,
            getattr(body, "group_ids", None),
        )
