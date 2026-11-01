from __future__ import annotations

from types import SimpleNamespace

from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage

from octop.infra.agents.middleware import output_guard
from octop.infra.agents.middleware.output_guard import (
    OutputGuardMiddleware,
    guard_message,
    split_clarifying_question,
    strip_narration,
)


def test_strips_leading_and_trailing_narration() -> None:
    text = "我已获得结果，现在为你整理。\n\n本月收入 120 万元，环比增长 8%。\n\n以上就是结果"
    assert strip_narration(text).startswith("本月收入 120 万元")
    assert strip_narration("Now let me summarize the findings:\n\nRevenue grew 8%.") == (
        "Revenue grew 8%."
    )


def test_keeps_body_and_never_returns_empty() -> None:
    body = "第一步：打开设置。\n现在为你整理一下注意事项：\n第二步：保存。"
    assert strip_narration(body) == body
    assert strip_narration("现在为你整理。") == "现在为你整理。"


def test_short_question_becomes_clarify_card() -> None:
    message = AIMessage(content="我可以帮你查报表。\n\n你要查哪个月份的报表？")
    guarded = guard_message(message, can_ask=True)
    assert guarded.content == "我可以帮你查报表。"
    assert guarded.tool_calls[0]["name"] == "ask_user_question"
    assert guarded.tool_calls[0]["args"]["questions"][0]["question"] == "你要查哪个月份的报表？"


def test_courtesy_and_long_answers_are_left_alone() -> None:
    assert split_clarifying_question("已完成。\n\n还有什么需要帮助的吗？") is None
    assert split_clarifying_question("Done.\n\nAnything else I can help with?") is None
    long_answer = "结论。" * 200 + "\n\n要按季度再拆一下吗？"
    assert split_clarifying_question(long_answer) is None


def test_no_card_without_ask_tool_or_with_tool_calls() -> None:
    message = AIMessage(content="你要查哪个月份？")
    assert guard_message(message, can_ask=False).tool_calls == []
    calling = AIMessage(
        content="你要查哪个月份？",
        tool_calls=[{"name": "search", "args": {}, "id": "c1", "type": "tool_call"}],
    )
    assert guard_message(calling, can_ask=True) is calling


def test_middleware_rewrites_final_message() -> None:
    middleware = OutputGuardMiddleware()
    tool = SimpleNamespace(name="ask_user_question")
    request = ModelRequest(model=SimpleNamespace(), messages=[HumanMessage(content="hi")])
    request = request.override(tools=[tool])

    response = middleware.wrap_model_call(
        request,
        lambda _req: ModelResponse(result=[AIMessage(content="你要查哪个账套？")]),
    )

    assert response.result[0].tool_calls[0]["name"] == "ask_user_question"


def test_data_bearing_result_line_is_kept() -> None:
    text = "已获得结果：9 月营收 120 万元，环比 +8%\n\n明细如下。"
    assert strip_narration(text) == text


def test_leading_blank_lines_do_not_hide_narration() -> None:
    assert strip_narration("\n\n我已获得结果，现在输出。\n\n正文第一段。") == "正文第一段。"


def test_unattended_run_keeps_question_as_text(monkeypatch) -> None:
    middleware = OutputGuardMiddleware()
    tool = SimpleNamespace(name="ask_user_question")
    request = ModelRequest(model=SimpleNamespace(), messages=[HumanMessage(content="hi")])
    request = request.override(tools=[tool])
    monkeypatch.setattr(output_guard, "_unattended", lambda _request: True)

    response = middleware.wrap_model_call(
        request,
        lambda _req: ModelResponse(result=[AIMessage(content="报告已生成。要附上明细吗？")]),
    )

    assert response.result[0].tool_calls == []
    assert response.result[0].content == "报告已生成。要附上明细吗？"
