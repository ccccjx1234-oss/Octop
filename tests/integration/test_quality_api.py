"""AI quality endpoints: feedback, model health, pricing / cost, evals, tool permissions."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from octop.infra.db.repos.thread_messages import ThreadMessageInput
from tests.support.auth import resolve_user_id


@pytest.fixture
async def env(env_usage):
    _c, srv, _admin_auth, _alice_auth, ctx = env_usage
    with srv.services.db.connect() as conn:
        now = int(time.time())
        conn.execute(
            "INSERT OR IGNORE INTO agents (agent_id, user_id, name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("agq", ctx["alice_id"], "Quality bot", now, now),
        )
    yield env_usage


def _seed_feedback_thread(srv: Any, *, thread_id: str, user_id: int) -> None:
    repos = srv.services.repos
    now = int(time.time())
    repos.thread_repo.insert(
        thread_id=thread_id,
        agent_id="agq",
        user_id=user_id,
        channel_type="dashboard",
        session_key=f"agq:dashboard:{user_id}:dm",
    )
    repos.thread_message_repo.append_if_ready(
        thread_id,
        [
            ThreadMessageInput(
                message_id="m1",
                role="ai",
                message_json=json.dumps({"type": "ai", "data": {"content": "x"}}),
                created_at=now,
            )
        ],
    )


async def test_feedback_round_trip_and_admin_summary(env: Any) -> None:
    c, srv, admin_auth, alice_auth, ctx = env
    _seed_feedback_thread(srv, thread_id="t1", user_id=ctx["alice_id"])
    url = "/api/agents/agq/threads/t1/messages/m1/feedback"
    r = await c.put(
        url, json={"rating": -1, "comment": "wrong", "excerpt": "x"}, headers=alice_auth
    )
    assert r.status_code == 204, r.text

    r = await c.get("/api/agents/agq/threads/t1/feedback", headers=alice_auth)
    assert r.json() == {"ratings": {"m1": -1}}

    r = await c.get("/api/admin/quality/feedback", headers=admin_auth)
    body = r.json()
    assert body["down"] == 1
    assert body["recent_down"][0]["comment"] == "wrong"

    r = await c.get("/api/admin/quality/feedback", headers=alice_auth)
    assert r.status_code == 403


async def test_pricing_and_cost(env: Any) -> None:
    c, srv, admin_auth, _alice_auth, ctx = env
    srv.services.usage_repo.record(
        agent_id="agq",
        user_id=ctx["alice_id"],
        model="openai:gpt-4o-mini",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    r = await c.put(
        "/api/admin/quality/pricing",
        json={"model": "gpt-4o-mini", "input_per_mtok": 1, "output_per_mtok": 4},
        headers=admin_auth,
    )
    assert r.status_code == 204, r.text
    r = await c.get("/api/admin/quality/cost", headers=admin_auth)
    assert r.json()["total_cost"] == 5.0
    r = await c.delete(
        "/api/admin/quality/pricing", params={"model": "gpt-4o-mini"}, headers=admin_auth
    )
    assert r.status_code == 204
    r = await c.get("/api/admin/quality/pricing", headers=admin_auth)
    assert r.json() == []


async def test_model_health_and_fallback_chain(env: Any) -> None:
    c, srv, admin_auth, _alice_auth, _ctx = env
    for provider, model in (("p", "a"), ("q", "b")):
        srv.services.repos.provider_repo.create(
            name=provider,
            kind="openai",
            base_url=f"https://{provider}.example/v1",
            api_key=f"sk-{provider}",
            models_json=json.dumps(
                [{"id": model, "name": model, "enabled": True, "input": ["text"]}]
            ),
        )
    r = await c.put(
        "/api/admin/quality/fallback-chain", json={"refs": ["p/a", "q/b"]}, headers=admin_auth
    )
    assert r.json() == {"fallback_chain": ["p/a", "q/b"]}
    r = await c.get("/api/admin/quality/model-health", headers=admin_auth)
    assert r.json()["fallback_chain"] == ["p/a", "q/b"]
    assert isinstance(r.json()["models"], list)
    r = await c.put("/api/admin/quality/fallback-chain", json={"refs": ["bad"]}, headers=admin_auth)
    assert r.status_code == 400


async def test_eval_cases_crud(env: Any) -> None:
    c, _srv, admin_auth, _alice_auth, _ctx = env
    r = await c.post(
        "/api/admin/quality/eval-cases",
        json={"suite": "smoke", "prompt": "hi", "expect_contains": ["hello"]},
        headers=admin_auth,
    )
    assert r.status_code == 201, r.text
    case_id = r.json()["eval_case_id"]
    r = await c.put(
        f"/api/admin/quality/eval-cases/{case_id}",
        json={"suite": "smoke", "prompt": "hello?", "expect_not_contains": ["sorry"]},
        headers=admin_auth,
    )
    assert r.json()["expect_not_contains"] == ["sorry"]
    r = await c.post(
        "/api/admin/quality/eval-runs",
        json={"agent_id": "agq", "suite": "empty"},
        headers=admin_auth,
    )
    assert r.status_code == 404
    r = await c.delete(f"/api/admin/quality/eval-cases/{case_id}", headers=admin_auth)
    assert r.status_code == 204
    r = await c.get("/api/admin/quality/eval-cases", headers=admin_auth)
    assert r.json() == []


async def test_tool_permissions(env: Any) -> None:
    c, srv, admin_auth, _alice_auth, _ctx = env
    group = srv.services.repos.user_group_repo.create(name="finance", description=None)
    r = await c.put(
        "/api/admin/quality/tool-permissions/execute",
        json={"group_ids": [group.user_group_id]},
        headers=admin_auth,
    )
    assert r.json()["visibility"] == "groups"
    r = await c.get("/api/admin/quality/tool-permissions", headers=admin_auth)
    tools = {t["tool_name"]: t for t in r.json()["tools"]}
    assert tools["execute"]["group_ids"] == [group.user_group_id]
    assert tools["read_file"]["group_ids"] == []
    r = await c.put(
        "/api/admin/quality/tool-permissions/execute", json={"group_ids": []}, headers=admin_auth
    )
    assert r.json()["visibility"] == "all"


async def test_feedback_rejects_unknown_message(env: Any) -> None:
    c, srv, _admin_auth, alice_auth, ctx = env
    _seed_feedback_thread(srv, thread_id="t1", user_id=ctx["alice_id"])
    url = "/api/agents/agq/threads/t1/messages/not-a-message/feedback"

    r = await c.put(url, json={"rating": -1, "excerpt": ""}, headers=alice_auth)
    assert r.status_code == 404, r.text

    # Clearing a vote for an unknown message is idempotent.
    r = await c.put(url, json={"rating": 0}, headers=alice_auth)
    assert r.status_code == 204, r.text


async def test_feedback_rejects_another_users_thread(env: Any) -> None:
    c, srv, admin_auth, alice_auth, _ctx = env
    admin_id = await resolve_user_id(c, admin_auth, "admin")
    _seed_feedback_thread(srv, thread_id="t-admin", user_id=admin_id)

    r = await c.put(
        "/api/agents/agq/threads/t-admin/messages/m1/feedback",
        json={"rating": 1, "excerpt": "x"},
        headers=alice_auth,
    )

    assert r.status_code == 404, r.text


async def test_fallback_chain_validation_uses_argument_code(env: Any) -> None:
    c, _srv, admin_auth, _alice_auth, _ctx = env
    r = await c.put(
        "/api/admin/quality/fallback-chain",
        json={"refs": ["bad"]},
        headers=admin_auth,
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "INVALID_ARGUMENT"
