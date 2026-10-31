"""AI quality: message feedback, model health and cost, eval runs, tool permissions.

``router`` holds per-user feedback routes under ``/api/agents``. ``admin_router``
holds the admin AI-quality page under ``/api/admin/quality``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from octop.api.common.agent import assert_agent_access
from octop.api.deps import current_user, get_server, require_admin
from octop.infra.agents.providers.fallback_chain import load_fallback_chain, save_fallback_chain
from octop.infra.agents.settings.tool_catalog import BUILTIN_TOOL_CATALOG
from octop.infra.db.services import RepoBundle
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.quality.evals import clean_case_fields, start_eval_run
from octop.infra.quality.feedback import MAX_COMMENT, quality_summary, record_feedback
from octop.infra.quality.pricing import cost_report, save_price
from octop.infra.users.groups import RESOURCE_TOOL, VISIBILITY_ALL, VISIBILITY_GROUPS, GroupAccess

router = APIRouter()
admin_router = APIRouter()


def _repos(server: Any) -> RepoBundle:
    repos: RepoBundle = server.services.repos
    return repos


def _registry(server: Any) -> Any:
    return server.app_runtime.agent_registry


def _timezone(server: Any) -> str:
    config = getattr(server, "config", None)
    return str(getattr(config, "default_timezone", "") or "UTC")


# --- feedback (any signed-in user) -----------------------------------------


class FeedbackBody(BaseModel):
    rating: int = Field(..., description="1 = thumbs up, -1 = thumbs down, 0 = clear the vote.")
    comment: str = Field("", max_length=MAX_COMMENT, description="Optional reason.")
    excerpt: str = Field("", description="Start of the rated answer, kept for the admin view.")


class ThreadFeedback(BaseModel):
    ratings: dict[str, int] = Field(..., description="message_id → rating for the current user.")


@router.put(
    "/agents/{agent_id}/threads/{thread_id}/messages/{message_id}/feedback",
    status_code=204,
    summary="Rate an assistant message",
)
async def put_feedback(
    agent_id: str,
    thread_id: str,
    message_id: str,
    body: FeedbackBody,
    server: Any = Depends(get_server),
    user: Any = Depends(current_user),
) -> Response:
    assert_agent_access(server, agent_id, user)
    record_feedback(
        _repos(server),
        agent_id=agent_id,
        thread_id=thread_id,
        message_id=message_id,
        user_id=int(user.id),
        rating=body.rating,
        comment=body.comment,
        excerpt=body.excerpt,
    )
    return Response(status_code=204)


@router.get(
    "/agents/{agent_id}/threads/{thread_id}/feedback",
    response_model=ThreadFeedback,
    summary="My ratings in one thread",
)
async def get_thread_feedback(
    agent_id: str,
    thread_id: str,
    server: Any = Depends(get_server),
    user: Any = Depends(current_user),
) -> ThreadFeedback:
    assert_agent_access(server, agent_id, user)
    ratings = _repos(server).message_feedback_repo.ratings_for_thread(
        agent_id=agent_id, thread_id=thread_id, user_id=int(user.id)
    )
    return ThreadFeedback(ratings=ratings)


# --- admin: quality summary ------------------------------------------------


@admin_router.get("/feedback", summary="Feedback summary per expert")
async def admin_feedback(
    days: int = Query(30, ge=1, le=365),
    server: Any = Depends(get_server),
    _: Any = Depends(require_admin()),
) -> dict[str, Any]:
    return quality_summary(_repos(server), days=days)


# --- admin: model health, fallback chain, pricing, cost -------------------


class FallbackChainBody(BaseModel):
    refs: list[str] = Field(
        ..., description="Ordered provider/model refs tried after a pinned model fails."
    )


@admin_router.get("/model-health", summary="Model availability and fallback chain")
async def admin_model_health(
    server: Any = Depends(get_server),
    _: Any = Depends(require_admin()),
) -> dict[str, Any]:
    return {
        "models": _registry(server).model_health_overview(),
        "fallback_chain": load_fallback_chain(_repos(server).settings_repo),
    }


@admin_router.put("/fallback-chain", summary="Replace the model fallback chain")
async def admin_put_fallback_chain(
    body: FallbackChainBody,
    server: Any = Depends(get_server),
    _: Any = Depends(require_admin()),
) -> dict[str, Any]:
    return {"fallback_chain": save_fallback_chain(_repos(server).settings_repo, body.refs)}


class PriceBody(BaseModel):
    model: str = Field(..., description="provider/model ref, or a bare model id.")
    input_per_mtok: float = Field(0, ge=0, description="Price per million uncached input tokens.")
    output_per_mtok: float = Field(0, ge=0, description="Price per million output tokens.")
    cache_read_per_mtok: float = Field(0, ge=0, description="Price per million cached tokens.")
    currency: str = Field("CNY", max_length=8)


@admin_router.get("/pricing", summary="List model prices")
async def admin_pricing(
    server: Any = Depends(get_server),
    _: Any = Depends(require_admin()),
) -> list[dict[str, Any]]:
    return [vars(row) for row in _repos(server).model_pricing_repo.list_all()]


@admin_router.put("/pricing", status_code=204, summary="Create or update a model price")
async def admin_put_pricing(
    body: PriceBody,
    server: Any = Depends(get_server),
    _: Any = Depends(require_admin()),
) -> Response:
    save_price(
        _repos(server),
        model=body.model,
        input_per_mtok=body.input_per_mtok,
        output_per_mtok=body.output_per_mtok,
        cache_read_per_mtok=body.cache_read_per_mtok,
        currency=body.currency,
    )
    return Response(status_code=204)


@admin_router.delete("/pricing", status_code=204, summary="Delete a model price")
async def admin_delete_pricing(
    model: str = Query(..., description="Model key to delete."),
    server: Any = Depends(get_server),
    _: Any = Depends(require_admin()),
) -> Response:
    _repos(server).model_pricing_repo.delete(model)
    return Response(status_code=204)


@admin_router.get("/cost", summary="Cost per model and per user")
async def admin_cost(
    window: str = Query("last_30d", description="Same windows as the usage summary."),
    server: Any = Depends(get_server),
    _: Any = Depends(require_admin()),
) -> dict[str, Any]:
    try:
        return cost_report(_repos(server), window=window, timezone=_timezone(server))
    except ValueError as exc:
        raise OctopError(ErrorCode.FORBIDDEN, str(exc), status=400) from exc


# --- admin: evals ----------------------------------------------------------


class EvalCaseBody(BaseModel):
    suite: str = Field("default", description="Suite name; a run executes one suite.")
    prompt: str = Field(..., description="Message sent to the expert.")
    expect_contains: list[str] = Field(
        default_factory=list, description="Phrases that must appear."
    )
    expect_not_contains: list[str] = Field(
        default_factory=list, description="Phrases that must not appear."
    )


class EvalRunBody(BaseModel):
    agent_id: str
    suite: str = "default"


def _case_fields(body: EvalCaseBody) -> dict[str, Any]:
    suite, prompt, contains, not_contains = clean_case_fields(
        suite=body.suite,
        prompt=body.prompt,
        expect_contains=body.expect_contains,
        expect_not_contains=body.expect_not_contains,
    )
    return {
        "suite": suite,
        "prompt": prompt,
        "expect_contains": contains,
        "expect_not_contains": not_contains,
    }


@admin_router.get("/eval-cases", summary="List eval cases")
async def admin_eval_cases(
    server: Any = Depends(get_server),
    _: Any = Depends(require_admin()),
) -> list[dict[str, Any]]:
    return [vars(row) for row in _repos(server).eval_repo.list_cases()]


@admin_router.post("/eval-cases", status_code=201, summary="Create an eval case")
async def admin_create_eval_case(
    body: EvalCaseBody,
    server: Any = Depends(get_server),
    user: Any = Depends(require_admin()),
) -> dict[str, Any]:
    row = _repos(server).eval_repo.create_case(**_case_fields(body), created_by=int(user.id))
    return vars(row)


@admin_router.put("/eval-cases/{eval_case_id}", summary="Update an eval case")
async def admin_update_eval_case(
    eval_case_id: str,
    body: EvalCaseBody,
    server: Any = Depends(get_server),
    _: Any = Depends(require_admin()),
) -> dict[str, Any]:
    row = _repos(server).eval_repo.update_case(eval_case_id, **_case_fields(body))
    if row is None:
        raise OctopError(ErrorCode.NOT_FOUND, "eval case not found")
    return vars(row)


@admin_router.delete("/eval-cases/{eval_case_id}", status_code=204, summary="Delete an eval case")
async def admin_delete_eval_case(
    eval_case_id: str,
    server: Any = Depends(get_server),
    _: Any = Depends(require_admin()),
) -> Response:
    _repos(server).eval_repo.delete_case(eval_case_id)
    return Response(status_code=204)


@admin_router.get("/eval-runs", summary="Recent eval runs")
async def admin_eval_runs(
    server: Any = Depends(get_server),
    _: Any = Depends(require_admin()),
) -> list[dict[str, Any]]:
    return [vars(row) for row in _repos(server).eval_repo.list_runs()]


@admin_router.post(
    "/eval-runs",
    status_code=202,
    summary="Run an eval suite against an expert",
    description="Runs in the background; poll the run until status is done or failed.",
)
async def admin_start_eval_run(
    body: EvalRunBody,
    server: Any = Depends(get_server),
    user: Any = Depends(require_admin()),
) -> dict[str, Any]:
    run = start_eval_run(
        _registry(server),
        _repos(server),
        agent_id=body.agent_id,
        suite=body.suite.strip() or "default",
        user_id=int(user.id),
    )
    return vars(run)


@admin_router.get("/eval-runs/{eval_run_id}", summary="One eval run with per-case results")
async def admin_eval_run(
    eval_run_id: str,
    server: Any = Depends(get_server),
    _: Any = Depends(require_admin()),
) -> dict[str, Any]:
    repo = _repos(server).eval_repo
    run = repo.get_run(eval_run_id)
    if run is None:
        raise OctopError(ErrorCode.NOT_FOUND, "eval run not found")
    return {**vars(run), "results": [vars(r) for r in repo.list_results(eval_run_id)]}


# --- admin: tool permissions -----------------------------------------------


class ToolPermissionBody(BaseModel):
    group_ids: list[str] = Field(
        default_factory=list,
        description="User groups allowed to call the tool. Empty = everyone (no restriction).",
    )


@admin_router.get("/tool-permissions", summary="Tools and the groups allowed to call them")
async def admin_tool_permissions(
    server: Any = Depends(get_server),
    _: Any = Depends(require_admin()),
) -> dict[str, Any]:
    grants = _repos(server).resource_group_grant_repo.group_ids_by_resource(RESOURCE_TOOL)
    names = [entry.name for entry in BUILTIN_TOOL_CATALOG]
    names += sorted(name for name in grants if name not in names)
    return {
        "tools": [
            {
                "tool_name": name,
                "builtin": i < len(BUILTIN_TOOL_CATALOG),
                "group_ids": sorted(grants.get(name, set())),
            }
            for i, name in enumerate(names)
        ]
    }


@admin_router.put("/tool-permissions/{tool_name}", summary="Restrict a tool to user groups")
async def admin_put_tool_permission(
    tool_name: str,
    body: ToolPermissionBody,
    server: Any = Depends(get_server),
    _: Any = Depends(require_admin()),
) -> dict[str, Any]:
    name = tool_name.strip()
    if not name:
        raise OctopError(ErrorCode.FORBIDDEN, "tool name is required", status=400)
    access = GroupAccess(_repos(server))
    visibility = VISIBILITY_GROUPS if body.group_ids else VISIBILITY_ALL
    access.set_visibility(RESOURCE_TOOL, name, visibility, body.group_ids)
    return {"tool_name": name, **access.visibility(RESOURCE_TOOL, name, shared=None)}
