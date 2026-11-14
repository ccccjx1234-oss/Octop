"""HTTP endpoints for the business workbench (progress, deliverables, dashboard)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request

from octop.api.deps import current_user, get_server
from octop.infra.users.identity import User
from octop.infra.workbench import WorkbenchService

router = APIRouter()


def _service(request: Request) -> WorkbenchService:
    server = get_server(request)
    return WorkbenchService(server.services)


@router.get("/workbench/progress")
def get_workbench_progress(
    request: Request,
    role: str = Query(default="customer_service"),
    range: str = Query(default="week"),  # noqa: A002
    user: User = Depends(current_user),
) -> dict[str, Any]:
    return _service(request).get_progress(user.id, role_key=role, period_range=range)


@router.post("/workbench/tasks")
def create_workbench_task(
    request: Request,
    payload: dict[str, Any] = Body(...),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    return _service(request).create_task(user.id, payload)


@router.patch("/workbench/tasks/{task_id}")
def update_workbench_task(
    request: Request,
    task_id: int,
    payload: dict[str, Any] = Body(...),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    return _service(request).update_task(user.id, task_id, payload)


@router.delete("/workbench/tasks/{task_id}")
def delete_workbench_task(
    request: Request,
    task_id: int,
    user: User = Depends(current_user),
) -> dict[str, bool]:
    _service(request).delete_task(user.id, task_id)
    return {"ok": True}


@router.get("/workbench/deliverables")
def list_workbench_deliverables(
    request: Request,
    type: str = Query(default="all"),  # noqa: A002
    q: str = Query(default=""),
    review_status: str = Query(default="all"),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    return _service(request).list_deliverables(
        user.id, deliverable_type=type, query=q, review_status=review_status
    )


@router.post("/workbench/deliverables")
def create_workbench_deliverable(
    request: Request,
    payload: dict[str, Any] = Body(...),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    return _service(request).create_deliverable(user.id, payload)


@router.patch("/workbench/deliverables/{deliverable_id}")
def update_workbench_deliverable(
    request: Request,
    deliverable_id: int,
    payload: dict[str, Any] = Body(...),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    return _service(request).update_deliverable(user.id, deliverable_id, payload)


@router.delete("/workbench/deliverables/{deliverable_id}")
def delete_workbench_deliverable(
    request: Request,
    deliverable_id: int,
    user: User = Depends(current_user),
) -> dict[str, bool]:
    _service(request).delete_deliverable(user.id, deliverable_id)
    return {"ok": True}


@router.get("/workbench/dashboard")
def get_workbench_dashboard(
    request: Request,
    role: str = Query(default="business"),
    range: str = Query(default="7d"),  # noqa: A002
    user: User = Depends(current_user),
) -> dict[str, Any]:
    return _service(request).get_dashboard(user.id, role_key=role, period_range=range)


@router.patch("/workbench/goals/{goal_id}")
def update_workbench_goal(
    request: Request,
    goal_id: int,
    payload: dict[str, Any] = Body(...),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    return _service(request).update_goal(user.id, goal_id, payload)


@router.post("/workbench/sync-mcn")
def sync_workbench_mcn(
    request: Request,
    payload: dict[str, Any] = Body(default_factory=dict),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    role_key = str(payload.get("role_key") or "customer_service")
    return _service(request).sync_mcn_orders(user.id, role_key=role_key)
