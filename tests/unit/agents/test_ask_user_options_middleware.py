"""Tests for AskUserOptionsMiddleware and connector write-interrupt wiring."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from langchain.agents.middleware import ModelRequest
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from octop_harness.builtin.tools.ask_user import (
    ASK_USER_TOOL_NAME,
    HEADLESS_FALLBACK,
    ask_user_question,
)

from octop.infra.agents.manager import AgentManager
from octop.infra.agents.middleware.ask_user_options import (
    MAX_OPTIONS,
    MAX_QUESTIONS,
    AskUserOptionsMiddleware,
)


def _request(tools: list[Any], system_text: str = "Base prompt") -> ModelRequest[Any]:
    return ModelRequest(
        model=MagicMock(),
        messages=[HumanMessage(content="hi")],
        system_message=SystemMessage(content=system_text),
        tools=tools,
    )


def test_ask_user_options_middleware_expands_schema() -> None:
    mw = AskUserOptionsMiddleware()
    captured: list[ModelRequest[Any]] = []

    def handler(req: ModelRequest[Any]) -> str:
        captured.append(req)
        return "ok"

    mw.wrap_model_call(_request([ask_user_question]), handler)

    modified = captured[0]
    ask_tool = next(t for t in modified.tools if t.name == ASK_USER_TOOL_NAME)
    schema = ask_tool.args_schema
    assert schema is not None

    questions = [
        {
            "header": "客户",
            "question": "请选择客户",
            "options": [
                {"label": f"客户{i}", "description": f"联系人{i}"} for i in range(MAX_OPTIONS)
            ],
        }
        for _ in range(MAX_QUESTIONS)
    ]
    parsed = schema.model_validate({"questions": questions})
    assert len(parsed.questions) == MAX_QUESTIONS
    assert len(parsed.questions[0].options) == MAX_OPTIONS


def test_ask_user_options_middleware_headless_fallback() -> None:
    mw = AskUserOptionsMiddleware()
    ask_req = SimpleNamespace(
        tool_call={
            "id": "call-ask-1",
            "name": ASK_USER_TOOL_NAME,
            "args": {
                "questions": [
                    {"question": "pick", "options": [{"label": str(i)} for i in range(10)]}
                ]
            },
        }
    )
    result = mw.wrap_tool_call(
        ask_req, lambda _r: ToolMessage(content="should not run", tool_call_id="x")
    )
    assert isinstance(result, ToolMessage)
    assert result.content == HEADLESS_FALLBACK
    assert result.tool_call_id == "call-ask-1"


def test_agent_manager_connector_write_interrupts_keeps_agently_without_duplicating_mcn() -> None:
    fake_manager = object.__new__(AgentManager)
    fake_repo = MagicMock()
    fake_repo.connector_feature_allows.return_value = True
    fake_repo.list_visible.return_value = [
        SimpleNamespace(kind="agently-cli", status="active", mcp_server_name="agently"),
        SimpleNamespace(kind="mcn", status="active", mcp_server_name="mcn"),
        SimpleNamespace(kind="mcn", status="disabled", mcp_server_name="mcn_disabled"),
    ]
    fake_manager._repos = SimpleNamespace(connector_repo=fake_repo)  # type: ignore[attr-defined]

    interrupts = fake_manager._connector_write_interrupts(42)
    assert any(k.startswith("agently_") for k in interrupts)
    assert "mcn_youli_create_order_from_quote" not in interrupts
