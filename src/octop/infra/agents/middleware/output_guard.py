"""Deterministic clean-up of the model's final answer.

Two guards run on a model response that has no tool calls (the final answer):

1. **Narration strip** — drop standalone lines at the start or end that only
   narrate the agent's own process ("我已获得结果，现在输出", "Now let me
   summarize:"). Body text is never touched.
2. **Text question → card** — a short answer that ends by asking the user a
   question becomes an ``ask_user_question`` tool call, so the user answers on
   a card and the turn resumes exactly where it paused. Courtesy endings
   ("还有什么需要帮助的吗？", "Anything else?") are left alone.

Both rewrite the message stored in thread state and history. Tokens already
streamed live are not recalled; a refresh shows the cleaned message.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage
from octop_harness.builtin.tools.ask_user import ASK_USER_TOOL_NAME
from octop_harness.middleware.runtime import runtime_config

_NARRATION = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^(好的[,，]?\s*)?我?(已经|已)(获得|拿到|得到|获取|查到)(了)?.{0,12}(结果|数据|信息|内容)"
        r"(\s*[，,。！!：:]?\s*(现在|接下来|下面)?\s*(我)?(来|将|为你|给你|为您|给您)?"
        r"(整理|输出|总结|回答|生成|汇总|展示|汇报|说明).{0,20})?$",
        r"^(现在|接下来|下面)[,，]?\s*(我|让我)?(来|将|开始|马上)?(为你|给你|为您|给您)?"
        r"(整理|输出|总结|回答|生成|汇总|展示).{0,20}[:：。.!！]?$",
        r"^(让我|我来|我先)(来)?(整理|总结|汇总|看看|查看|分析).{0,20}[:：。.]$",
        r"^(now|next),?\s+(i('| wi)ll|let me|i am going to)\b.{0,80}[:.]?$",
        r"^i (now )?have (the|all the|enough) (results|information|data)\b.{0,80}$",
        r"^let me (now )?(summari[sz]e|compile|put together|format|present)\b.{0,60}[:.]$",
    )
]
_COURTESY = re.compile(
    r"(还有(什么|其他|别的).{0,12}(吗|么)|有什么.{0,8}(帮|需要)|需要我.{0,24}(吗|么)|"
    r"要不要我|是否需要|随时(告诉|联系)|"
    r"anything else|let me know|would you like me|do you want me|shall i|"
    r"is there anything|can i help)",
    re.IGNORECASE,
)
_QUESTION_END = re.compile(r"[?？]\s*$")
MAX_NARRATION_LINES = 2
# Long answers that end with a question are follow-up offers, not clarifications.
MAX_CLARIFY_ANSWER_CHARS = 400
MAX_QUESTION_CHARS = 300


def _is_narration(line: str) -> bool:
    text = line.strip().strip("*_#> ").strip()
    return bool(text) and len(text) <= 120 and any(p.match(text) for p in _NARRATION)


def strip_narration(text: str) -> str:
    """Drop narration-only lines at the head and tail of *text*."""
    lines = text.split("\n")
    head = 0
    dropped_head = 0
    while head < len(lines):
        if not lines[head].strip():
            head += 1
            continue
        if dropped_head >= MAX_NARRATION_LINES or not _is_narration(lines[head]):
            break
        dropped_head += 1
        head += 1
    tail = len(lines)
    dropped = 0
    while tail > head and dropped < MAX_NARRATION_LINES:
        line = lines[tail - 1]
        if line.strip() and not _is_narration(line):
            break
        if line.strip():
            dropped += 1
        tail -= 1
    kept = "\n".join(lines[head:tail]).strip()
    return kept if kept else text


def split_clarifying_question(text: str) -> tuple[str, str] | None:
    """Return ``(lead_text, question)`` when *text* ends by asking the user."""
    body = text.strip()
    if not body or len(body) > MAX_CLARIFY_ANSWER_CHARS or not _QUESTION_END.search(body):
        return None
    paragraphs = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    question = paragraphs[-1].strip()
    if len(question) > MAX_QUESTION_CHARS or "```" in question or _COURTESY.search(question):
        return None
    lead = "\n\n".join(paragraphs[:-1]).strip()
    return lead, question


def _text_of(message: AIMessage) -> str | None:
    content = message.content
    if isinstance(content, str):
        return content
    return None  # multi-part content (images, reasoning blocks): leave untouched


def _unattended(request: ModelRequest[Any]) -> bool:
    config = runtime_config(request)
    configurable = config.get("configurable") if isinstance(config, dict) else None
    return isinstance(configurable, dict) and configurable.get("octop_unattended") is True


def _tool_names(request: ModelRequest[Any]) -> set[str]:
    names: set[str] = set()
    for tool in request.tools or []:
        name = tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)
        if isinstance(name, str):
            names.add(name)
    return names


def guard_message(message: AIMessage, *, can_ask: bool) -> AIMessage:
    if message.tool_calls:
        return message
    text = _text_of(message)
    if text is None or not text.strip():
        return message
    cleaned = strip_narration(text)
    if can_ask:
        split = split_clarifying_question(cleaned)
        if split is not None:
            lead, question = split
            return message.model_copy(
                update={
                    "content": lead,
                    "tool_calls": [
                        {
                            "name": ASK_USER_TOOL_NAME,
                            "args": {"questions": [{"question": question, "options": []}]},
                            "id": f"call_guard_{uuid4().hex[:16]}",
                            "type": "tool_call",
                        }
                    ],
                }
            )
    if cleaned == text:
        return message
    return message.model_copy(update={"content": cleaned})


class OutputGuardMiddleware(AgentMiddleware[Any, Any]):
    def _apply(self, request: ModelRequest[Any], response: Any) -> Any:
        can_ask = ASK_USER_TOOL_NAME in _tool_names(request) and not _unattended(request)
        if isinstance(response, AIMessage):
            return guard_message(response, can_ask=can_ask)
        result = getattr(response, "result", None)
        if not isinstance(result, list):
            return response
        for index in range(len(result) - 1, -1, -1):
            message = result[index]
            if isinstance(message, AIMessage):
                result[index] = guard_message(message, can_ask=can_ask)
                break
        return response

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        return self._apply(request, handler(request))  # type: ignore[no-any-return]

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        return self._apply(request, await handler(request))  # type: ignore[no-any-return]


__all__ = [
    "OutputGuardMiddleware",
    "guard_message",
    "split_clarifying_question",
    "strip_narration",
]
