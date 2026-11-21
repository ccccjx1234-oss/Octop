"""Endpoints for Huihuo ERP organization directory & employee sync."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from octop.api.deps import current_user, get_server
from octop.infra.auth.youli.client import YouliClientError, YouliClientUnavailable
from octop.infra.auth.youli.org_sync import ErpOrgSyncService
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.identity import User
from octop.infra.users.profile_avatar import profile_avatar_url

router = APIRouter()


class SyncErpOrgBody(BaseModel):
    company_id: int | None = None


def _resolve_actor_company_id(
    user: User,
    org_repo: Any,
    requested_company_id: int | None,
) -> int | None:
    if user.is_admin:
        return requested_company_id
    ctx = org_repo.get_user_erp_context(user.id)
    if ctx is not None and ctx.company_id > 0:
        actor_cid = int(ctx.company_id)
        if (
            requested_company_id is not None
            and requested_company_id > 0
            and int(requested_company_id) != actor_cid
        ):
            raise OctopError(
                ErrorCode.FORBIDDEN,
                "cross-company access is not allowed",
                status=403,
            )
        return actor_cid
    return requested_company_id


def _summary_for_actor(user: User, org_repo: Any) -> dict[str, Any]:
    summary = org_repo.get_sync_summary()
    if user.is_admin:
        return summary
    eff_cid = _resolve_actor_company_id(user, org_repo, None)
    if eff_cid is not None and eff_cid > 0:
        depts = org_repo.list_departments(company_id=eff_cid)
        emps = org_repo.list_employees(company_id=eff_cid)
        synced_epochs = [int(d.synced_at or 0) for d in depts] + [
            int(e.get("synced_at") or 0) for e in emps
        ]
        max_synced = max((ts for ts in synced_epochs if ts > 0), default=None)
        known = summary.get("company_ids") or []
        return {
            "company_ids": [eff_cid] if (eff_cid in known or depts or emps) else [],
            "department_count": len(depts),
            "employee_count": len(emps),
            "last_synced_at": max_synced,
        }
    return summary


def _build_dept_tree(rows: list[Any]) -> list[dict[str, Any]]:
    nodes: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        key = (int(row.company_id), int(row.org_id))
        nodes[key] = {
            "company_id": int(row.company_id),
            "org_id": int(row.org_id),
            "parent_id": int(row.parent_id),
            "name": row.name,
            "org_code": row.org_code,
            "sort": int(row.sort),
            "status": int(row.status),
            "children": [],
        }
    roots: list[dict[str, Any]] = []
    for row in rows:
        node = nodes[(int(row.company_id), int(row.org_id))]
        parent_key = (int(row.company_id), int(row.parent_id))
        parent = nodes.get(parent_key)
        if parent is not None and parent_key != (int(row.company_id), int(row.org_id)):
            parent["children"].append(node)
        else:
            roots.append(node)
    return roots


@router.get("/status", summary="Get Huihuo ERP org directory sync summary")
async def get_erp_org_status(
    user: User = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    summary = _summary_for_actor(user, server.services.repos.erp_org_repo)
    cfg = server.services.config.youli
    summary["integration_enabled"] = bool(
        cfg.enabled and cfg.base_url and cfg.client_id and cfg.client_secret
    )
    return summary


@router.get("/departments", summary="List projected Huihuo ERP departments")
async def list_erp_departments(
    company_id: int | None = Query(default=None),
    include_inactive: bool = Query(default=False),
    user: User = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    org_repo = server.services.repos.erp_org_repo
    eff_cid = _resolve_actor_company_id(user, org_repo, company_id)
    rows = org_repo.list_departments(
        company_id=eff_cid,
        include_inactive=include_inactive,
    )
    flat = [
        {
            "company_id": int(r.company_id),
            "org_id": int(r.org_id),
            "parent_id": int(r.parent_id),
            "name": r.name,
            "org_code": r.org_code,
            "sort": int(r.sort),
            "status": int(r.status),
            "synced_at": int(r.synced_at),
        }
        for r in rows
    ]
    return {"departments": flat, "tree": _build_dept_tree(rows)}


@router.get("/employees", summary="List projected Huihuo ERP employees")
async def list_erp_employees(
    company_id: int | None = Query(default=None),
    org_id: int | None = Query(default=None),
    include_children: bool = Query(default=True),
    q: str = Query(default=""),
    active_only: bool = Query(default=True),
    user: User = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    org_repo = server.services.repos.erp_org_repo
    eff_cid = _resolve_actor_company_id(user, org_repo, company_id)
    employees = org_repo.list_employees(
        company_id=eff_cid,
        org_id=org_id,
        include_children=include_children,
        query=q,
        active_only=active_only,
    )
    return {"employees": employees}


@router.get("/local-users", summary="List enabled local Octop users for scope assignment")
async def list_local_users(
    q: str = Query(default=""),
    _: User = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    needle = q.strip().lower()
    rows = server.services.user_repo.list(include_disabled=False)
    avatars = server.services.avatars("user")
    items: list[dict[str, Any]] = []
    for row in rows:
        if getattr(row, "disabled", 0):
            continue
        username = str(row.username or "")
        display_name = str(row.display_name or "")
        if needle and needle not in username.lower() and needle not in display_name.lower():
            continue
        items.append(
            {
                "user_id": int(row.id),
                "username": username,
                "display_name": display_name or username,
                "role": str(row.role),
                "role_name": getattr(row, "role_name", None),
                "avatar_icon": getattr(row, "avatar_icon", None),
                "avatar_url": profile_avatar_url(
                    avatars, str(row.id), f"/api/users/{row.id}/avatar"
                ),
            }
        )
    return {"users": items}


@router.post("/sync", summary="Synchronize Huihuo ERP organization directory")
async def sync_erp_org(
    body: SyncErpOrgBody | None = None,
    user: User = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    cfg = server.services.config.youli
    svc = ErpOrgSyncService(server.services, cfg)
    if not svc.enabled:
        reason = "会火 ERP 组织同步未配置或未启用"
        raise OctopError(
            ErrorCode.NETWORK_ERROR,
            reason,
            status=503,
            details={"reason": reason},
        )
    org_repo = server.services.repos.erp_org_repo
    requested_cid = body.company_id if body is not None else None
    eff_cid = _resolve_actor_company_id(user, org_repo, requested_cid)
    target_ids: list[int] = []
    if eff_cid is not None and eff_cid > 0:
        target_ids = [int(eff_cid)]
    elif body is not None and body.company_id is not None and body.company_id > 0:
        target_ids = [int(body.company_id)]
    else:
        target_ids = org_repo.known_company_ids()
        if not target_ids:
            ctx = org_repo.get_user_erp_context(user.id)
            if ctx is not None and ctx.company_id > 0:
                target_ids = [int(ctx.company_id)]
    if not target_ids:
        reason = "请先输入 company_id 或至少使用会火员工账号登录一次后再同步"
        raise OctopError(
            ErrorCode.SLASH_BAD_ARGS,
            reason,
            status=400,
            details={"reason": reason},
        )
    results: list[dict[str, Any]] = []
    try:
        for cid in target_ids:
            results.append(await asyncio.to_thread(svc.sync_company, cid))
    except YouliClientUnavailable as exc:
        raise OctopError(
            ErrorCode.NETWORK_ERROR, str(exc), status=503, details={"reason": str(exc)}
        ) from exc
    except YouliClientError as exc:
        raise OctopError(
            ErrorCode.INTERNAL_ERROR, str(exc), status=502, details={"reason": str(exc)}
        ) from exc
    return {
        "results": results,
        "summary": _summary_for_actor(user, org_repo),
    }
