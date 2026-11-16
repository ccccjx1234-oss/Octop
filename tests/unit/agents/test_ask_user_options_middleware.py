"""Tests for AskUserOptionsMiddleware and MCN connector HITL interrupt wiring."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from langchain.agents.middleware import ModelRequest
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from octop_harness.builtin.tools.ask_user import (
    ASK_USER_TOOL_NAME,
    HEADLESS_FALLBACK,
    ask_user_question,
)

from octop.infra.agents.manager import AgentManager
from octop.infra.agents.middleware.ask_user_options import (
    _MCN_SYSTEM_MARKER,
    MAX_OPTIONS,
    MAX_QUESTIONS,
    AskUserOptionsMiddleware,
    McnWriteHitlMiddleware,
)
from octop.infra.agents.security.hitl_session import HitlSessionPolicyStore


def _dummy_mcn_tool(name: str = "mcn_youli_list_customers") -> StructuredTool:
    return StructuredTool.from_function(
        func=lambda keyword="": keyword,
        name=name,
        description="list customers",
    )


def _dummy_other_tool(name: str = "read_file") -> StructuredTool:
    return StructuredTool.from_function(
        func=lambda path="": path,
        name=name,
        description="read file",
    )


def _request(tools: list[Any], system_text: str = "Base prompt") -> ModelRequest[Any]:
    return ModelRequest(
        model=MagicMock(),
        messages=[HumanMessage(content="hi")],
        system_message=SystemMessage(content=system_text),
        tools=tools,
    )


def test_ask_user_options_middleware_expands_schema_and_injects_mcn_guidance() -> None:
    mw = AskUserOptionsMiddleware()
    captured: list[ModelRequest[Any]] = []

    def handler(req: ModelRequest[Any]) -> str:
        captured.append(req)
        return "ok"

    mw.wrap_model_call(_request([ask_user_question, _dummy_mcn_tool()]), handler)

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

    sys_text = str(modified.system_message.content)
    assert _MCN_SYSTEM_MARKER in sys_text
    assert "最多 6 题" in sys_text
    assert "最多 20 个选项" in sys_text
    assert "必要字段缺失时必须持续引导到完整" in sys_text
    assert "禁止**在必要字段仍缺失时仅发纯文本" in sys_text

    # Calling again with an already-injected prompt stays idempotent.
    mw.wrap_model_call(
        _request([ask_user_question, _dummy_mcn_tool()], system_text=sys_text),
        handler,
    )
    second_sys = str(captured[1].system_message.content)
    assert second_sys.count(_MCN_SYSTEM_MARKER) == 1


def test_ask_user_options_middleware_skips_mcn_addendum_without_mcn_tools() -> None:
    mw = AskUserOptionsMiddleware()
    captured: list[ModelRequest[Any]] = []

    mw.wrap_model_call(
        _request([ask_user_question, _dummy_other_tool()], system_text="Plain"),
        lambda req: (captured.append(req), "ok")[1],
    )
    assert _MCN_SYSTEM_MARKER not in str(captured[0].system_message.content)


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


def test_mcn_write_hitl_middleware_gates_confirm_true_and_respects_session_policy() -> None:
    store = HitlSessionPolicyStore()
    mw = McnWriteHitlMiddleware(session_store=store)

    write_tool = "mcn__01M7DCJN46VS0783FT1ZFWW62H_youli_create_order_from_quote"
    read_tool = "mcn__01M7DCJN46VS0783FT1ZFWW62H_youli_list_customers"

    rule = mw.interrupt_on.get(write_tool)
    assert rule is not None
    assert write_tool in mw.interrupt_on
    assert mw.interrupt_on[write_tool]["allowed_decisions"] == ["approve", "reject"]
    assert read_tool not in mw.interrupt_on
    assert mw.interrupt_on.get(read_tool) is None

    when = rule["when"]
    assert (
        when(
            SimpleNamespace(tool_call={"name": write_tool, "args": {"confirm": True}}, runtime=None)
        )
        is True
    )
    assert (
        when(
            SimpleNamespace(
                tool_call={"name": write_tool, "args": {"confirm": False}}, runtime=None
            )
        )
        is False
    )

    store.set("thr-mcn", {"mode": "allow_tools", "tools": [write_tool]})
    req_bypassed = SimpleNamespace(
        tool_call={"name": write_tool, "args": {"confirm": True}},
        runtime=SimpleNamespace(config={"configurable": {"thread_id": "thr-mcn"}}),
    )
    assert when(req_bypassed) is False
