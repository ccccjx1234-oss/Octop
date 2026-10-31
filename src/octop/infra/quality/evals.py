"""Run an eval suite against one expert and record pass / fail per case.

Each case runs in its own throwaway thread (``eval:<run>:<case>``) so eval
traffic never lands in a user's chat list. Checks are deterministic: every
``expect_contains`` phrase must appear and no ``expect_not_contains`` phrase
may appear (case-insensitive). A turn that stops for user approval fails.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from octop.infra.db.repos.evals import EvalCaseRow, EvalRunRow
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.gateway.process import build_harness_request
from octop.infra.gateway.process.usage_record import UsageTracker, record_turn_usage
from octop.infra.utils.llm_text import strip_thinking

logger = logging.getLogger(__name__)

MAX_OUTPUT = 4000
CASE_TIMEOUT_S = 300.0
# Strong references so running eval tasks are not garbage-collected.
_RUNNING: set[asyncio.Task[None]] = set()


def check_output(case: EvalCaseRow, output: str) -> tuple[bool, str]:
    """Return ``(passed, detail)`` for one case output."""
    text = output.lower()
    missing = [p for p in case.expect_contains if p.strip() and p.lower() not in text]
    banned = [p for p in case.expect_not_contains if p.strip() and p.lower() in text]
    if not output.strip():
        return False, "empty answer"
    problems: list[str] = []
    if missing:
        problems.append("missing: " + ", ".join(missing))
    if banned:
        problems.append("contains banned: " + ", ".join(banned))
    return (not problems), "; ".join(problems)


def clean_case_fields(
    *, suite: str, prompt: str, expect_contains: list[str], expect_not_contains: list[str]
) -> tuple[str, str, list[str], list[str]]:
    suite_name = suite.strip() or "default"
    text = prompt.strip()
    if not text:
        raise OctopError(ErrorCode.FORBIDDEN, "prompt is required", status=400)
    contains = [p.strip() for p in expect_contains if p.strip()]
    not_contains = [p.strip() for p in expect_not_contains if p.strip()]
    return suite_name[:64], text, contains, not_contains


async def _run_case(
    agent_manager: Any,
    repos: Any,
    *,
    run: EvalRunRow,
    case: EvalCaseRow,
    user_id: int,
    model: str | None,
) -> None:
    thread_id = f"eval:{run.eval_run_id}:{case.eval_case_id}"
    request = build_harness_request(
        thread_id=thread_id,
        user_id=user_id,
        agent_id=run.agent_id,
        source="eval",
        text=case.prompt,
        model=model,
    )
    usage = UsageTracker()
    parts: list[str] = []
    needs_user = False
    started = time.monotonic()

    async def _consume() -> None:
        nonlocal needs_user
        async for chunk in agent_manager.stream(run.agent_id, request):
            usage.observe(chunk)
            kind = chunk.get("type")
            if kind in ("token", "delta"):
                parts.append(str(chunk.get("content") or chunk.get("text") or ""))
            elif kind == "hitl_required":
                needs_user = True

    detail = ""
    try:
        await asyncio.wait_for(_consume(), timeout=CASE_TIMEOUT_S)
    except TimeoutError:
        detail = "timed out"
    except Exception as exc:  # noqa: BLE001 - one broken case must not stop the run
        detail = f"error: {exc}"[:500]
    output = strip_thinking("".join(parts)).strip()
    if detail:
        passed = False
    elif needs_user:
        passed, detail = False, "stopped for user approval"
    else:
        passed, detail = check_output(case, output)
    if usage.usage is not None:
        record_turn_usage(
            repos.usage_repo,
            agent_id=run.agent_id,
            user_id=user_id,
            thread_id=thread_id,
            usage=usage.usage,
            source="eval",
        )
    repos.eval_repo.add_result(
        run.eval_run_id,
        eval_case_id=case.eval_case_id,
        prompt=case.prompt,
        passed=passed,
        output=output[:MAX_OUTPUT],
        detail=detail,
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    try:
        await agent_manager.delete_thread_checkpoint(run.agent_id, thread_id)
    except Exception:  # noqa: BLE001 - checkpoint cleanup is best effort
        logger.debug("eval checkpoint cleanup failed thread=%s", thread_id, exc_info=True)


async def _run_all(
    agent_manager: Any,
    repos: Any,
    *,
    run: EvalRunRow,
    cases: list[EvalCaseRow],
    user_id: int,
    model: str | None,
) -> None:
    try:
        for case in cases:
            await _run_case(agent_manager, repos, run=run, case=case, user_id=user_id, model=model)
    except Exception as exc:  # noqa: BLE001 - record the failure on the run row
        logger.exception("eval run failed run=%s", run.eval_run_id)
        repos.eval_repo.finish_run(run.eval_run_id, status="failed", error=str(exc)[:500])
        return
    repos.eval_repo.finish_run(run.eval_run_id, status="done")


def start_eval_run(
    agent_manager: Any,
    repos: Any,
    *,
    agent_id: str,
    suite: str,
    user_id: int,
) -> EvalRunRow:
    """Create a run row and execute its cases in the background."""
    cases = repos.eval_repo.list_cases(suite)
    if not cases:
        raise OctopError(ErrorCode.NOT_FOUND, "eval suite has no cases")
    agent_manager.get_agent(agent_id)  # raises when the expert is not running
    row = agent_manager.get_row(agent_id)
    model = (row.default_model if row is not None else None) or (
        agent_manager.resolve_fallback_model_ref()
    )
    run: EvalRunRow = repos.eval_repo.create_run(
        agent_id=agent_id, suite=suite, total=len(cases), started_by=user_id
    )
    task = asyncio.create_task(
        _run_all(agent_manager, repos, run=run, cases=cases, user_id=user_id, model=model)
    )
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    return run


__all__ = ["check_output", "clean_case_fields", "start_eval_run"]
