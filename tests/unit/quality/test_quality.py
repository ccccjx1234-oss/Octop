"""Model cost, message feedback, eval checks, and the per-tool permission gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from octop.infra.agents.middleware.tool_permission import tool_allowed
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.evals import EvalCaseRow
from octop.infra.db.services import RepoBundle
from octop.infra.errors import OctopError
from octop.infra.quality.evals import check_output, clean_case_fields
from octop.infra.quality.feedback import quality_summary, record_feedback
from octop.infra.quality.pricing import cost_report, save_price
from octop.infra.users.groups import RESOURCE_TOOL, GroupAccess


@pytest.fixture
def repos(tmp_path: Path) -> RepoBundle:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return RepoBundle.from_pool(pool)


def _user(repos: RepoBundle, username: str, role: str = "user") -> int:
    return repos.user_repo.create(username=username, role=role)


def test_cost_report_uses_exact_then_bare_model_price(repos: RepoBundle) -> None:
    ada = _user(repos, "ada")
    repos.agent_repo.create(agent_id="ag1", user_id=ada, name="Helper")
    repos.usage_repo.record(
        agent_id="ag1", user_id=ada, input_tokens=1_000_000, output_tokens=500_000, model="ds/chat"
    )
    repos.usage_repo.record(
        agent_id="ag1", user_id=ada, input_tokens=2_000_000, output_tokens=0, model="other/qwen"
    )
    save_price(
        repos,
        model="ds/chat",
        input_per_mtok=2,
        output_per_mtok=8,
        cache_read_per_mtok=0.5,
        currency="cny",
    )
    save_price(
        repos,
        model="qwen",
        input_per_mtok=1,
        output_per_mtok=1,
        cache_read_per_mtok=0,
        currency="CNY",
    )

    report = cost_report(repos, window="last_30d", timezone="UTC")

    assert report["currency"] == "CNY"
    assert report["total_cost"] == pytest.approx(2 + 4 + 2)
    assert {m["model"]: m["cost"] for m in report["by_model"]} == {
        "ds/chat": 6.0,
        "other/qwen": 2.0,
    }
    assert report["by_user"][0]["username"] == "ada"


def test_unpriced_model_is_flagged(repos: RepoBundle) -> None:
    ada = _user(repos, "ada")
    repos.agent_repo.create(agent_id="ag1", user_id=ada, name="Helper")
    repos.usage_repo.record(
        agent_id="ag1", user_id=ada, input_tokens=10, output_tokens=10, model="x/y"
    )
    report = cost_report(repos, window="last_30d", timezone="UTC")
    assert report["by_model"] == [
        {"model": "x/y", "total_tokens": 20, "cost": 0.0, "priced": False}
    ]
    with pytest.raises(OctopError):
        save_price(
            repos,
            model="x/y",
            input_per_mtok=-1,
            output_per_mtok=0,
            cache_read_per_mtok=0,
            currency="",
        )


def test_feedback_vote_update_clear_and_summary(repos: RepoBundle) -> None:
    ada = _user(repos, "ada")
    bob = _user(repos, "bob")
    repos.agent_repo.create(agent_id="ag1", user_id=ada, name="Helper")
    record_feedback(repos, agent_id="ag1", thread_id="t1", message_id="m1", user_id=ada, rating=1)
    record_feedback(
        repos,
        agent_id="ag1",
        thread_id="t1",
        message_id="m1",
        user_id=bob,
        rating=-1,
        comment="wrong month",
        excerpt="revenue was ...",
    )
    record_feedback(repos, agent_id="ag1", thread_id="t1", message_id="m2", user_id=ada, rating=-1)
    record_feedback(repos, agent_id="ag1", thread_id="t1", message_id="m2", user_id=ada, rating=0)

    assert repos.message_feedback_repo.ratings_for_thread(
        agent_id="ag1", thread_id="t1", user_id=ada
    ) == {"m1": 1}
    summary = quality_summary(repos, days=7)
    assert (summary["up"], summary["down"], summary["satisfaction"]) == (1, 1, 0.5)
    assert summary["per_agent"][0]["agent_name"] == "Helper"
    assert summary["recent_down"][0]["comment"] == "wrong month"
    with pytest.raises(OctopError):
        record_feedback(
            repos, agent_id="ag1", thread_id="t1", message_id="m3", user_id=ada, rating=5
        )


def _case(contains: list[str], not_contains: list[str]) -> EvalCaseRow:
    return EvalCaseRow(
        eval_case_id="c1",
        suite="s",
        prompt="p",
        expect_contains=contains,
        expect_not_contains=not_contains,
        created_at=0,
        updated_at=0,
    )


def test_eval_check_output() -> None:
    assert check_output(_case(["120 万"], ["抱歉"]), "本月收入 120 万元") == (True, "")
    passed, detail = check_output(_case(["Revenue"], ["sorry"]), "Sorry, no data")
    assert not passed
    assert "missing: Revenue" in detail and "contains banned: sorry" in detail
    assert check_output(_case([], []), "  ") == (False, "empty answer")
    assert clean_case_fields(
        suite=" ", prompt=" hi ", expect_contains=[" a ", ""], expect_not_contains=[]
    ) == ("default", "hi", ["a"], [])


def test_eval_run_counts_passes(repos: RepoBundle) -> None:
    ada = _user(repos, "ada")
    repos.agent_repo.create(agent_id="ag1", user_id=ada, name="Helper")
    case = repos.eval_repo.create_case(
        suite="smoke",
        prompt="hi",
        expect_contains=["hello"],
        expect_not_contains=[],
        created_by=ada,
    )
    run = repos.eval_repo.create_run(agent_id="ag1", suite="smoke", total=1, started_by=ada)
    repos.eval_repo.add_result(
        run.eval_run_id,
        eval_case_id=case.eval_case_id,
        prompt="hi",
        passed=True,
        output="hello",
        detail="",
        latency_ms=5,
    )
    repos.eval_repo.fail_stale_runs()
    stored = repos.eval_repo.get_run(run.eval_run_id)
    assert stored is not None
    assert (stored.passed, stored.status) == (1, "failed")
    assert repos.eval_repo.list_results(run.eval_run_id)[0].passed is True


def test_tool_gate_no_grant_allows_and_grant_narrows(repos: RepoBundle) -> None:
    admin = _user(repos, "root", role="admin")
    ada = _user(repos, "ada")
    bob = _user(repos, "bob")
    assert tool_allowed(repos, user_id=ada, tool_name="execute")

    finance = repos.user_group_repo.create(name="finance", description=None)
    repos.user_group_member_repo.add(finance.user_group_id, ada)
    GroupAccess(repos).set_visibility(RESOURCE_TOOL, "execute", "groups", [finance.user_group_id])

    assert tool_allowed(repos, user_id=ada, tool_name="execute")
    assert not tool_allowed(repos, user_id=bob, tool_name="execute")
    assert tool_allowed(repos, user_id=admin, tool_name="execute")
    assert tool_allowed(repos, user_id=bob, tool_name="read_file")
    assert tool_allowed(repos, user_id=None, tool_name="execute")

    repos.user_group_member_repo.remove(finance.user_group_id, ada)
    assert not tool_allowed(repos, user_id=ada, tool_name="execute")
