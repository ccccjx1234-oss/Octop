"""Extend ``ask_user_question`` so one card can hold more choices."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langchain_core.tools.base import BaseTool
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from octop_harness.builtin.tools.ask_user import ASK_USER_TOOL_NAME, HEADLESS_FALLBACK
from pydantic import BaseModel, Field, field_validator

MAX_QUESTIONS = 6
MAX_OPTIONS = 20

_EXTENDED_DESCRIPTION = (
    "Ask the user 1-6 structured questions via an interactive selection card and wait for their answer.\n\n"
    "Use this when:\n"
    "- Clarifying ambiguous requirements or mutually exclusive choices;\n"
    "- Selecting from real business/master-data candidates (e.g. MCN customers, opportunities, "
    "contracts, accounts, specs, projects, templates) — up to 20 options per question may be provided "
    "so the user can search and pick directly without truncation;\n"
    "- Continuing to collect remaining required fields (e.g. accounts, specs, quantities) across rounds "
    "until a write action can run — NEVER stop with plain text asking the user to reply when required fields are still missing;\n"
    "- Confirming complex multi-parameter setups before writing.\n\n"
    "Do NOT invent unseen options. If a list tool reports `hasMore=true`, mention totalCount/page in "
    "`question` and optionally add a '下一页' or keyword-filter option. Use `options=[]` only for genuinely open-ended text input. "
    "Do NOT duplicate a separate '是否确认提交' question right before calling a tool with `confirm=true` that already triggers "
    "a system approval card."
)


class ExtendedAskOption(BaseModel):
    """One selectable answer for the extended ``ask_user_question`` tool."""

    label: str = Field(
        ...,
        min_length=1,
        max_length=80,
        description="User-facing choice label (concise business name / identifier).",
    )
    description: str = Field(
        "",
        max_length=200,
        description="Optional short context such as customer contact, type, price, or tradeoff.",
    )


class ExtendedAskQuestion(BaseModel):
    """One interactive question supporting up to 20 options."""

    question: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="The question, phrased clearly in one step.",
    )
    header: str | None = Field(
        None,
        max_length=32,
        description="Optional short topic label, e.g. '选择客户'.",
    )
    options: list[ExtendedAskOption] = Field(
        default_factory=list,
        max_length=MAX_OPTIONS,
        description=(
            "2-20 choices (master-data lookups may include up to 20 real items per page). "
            "Leave empty only for open-ended questions."
        ),
    )
    multi_select: bool = Field(
        False,
        description="Allow selecting more than one option.",
    )

    @field_validator("question", "header")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value


class ExtendedAskUserQuestionArgs(BaseModel):
    """Arguments for the extended ``ask_user_question`` schema."""

    questions: list[ExtendedAskQuestion] = Field(
        ...,
        min_length=1,
        max_length=MAX_QUESTIONS,
        description="Ask everything needed in ONE call; up to 6 questions.",
    )


def _extended_ask_fallback(questions: list[Any]) -> str:
    del questions
    return HEADLESS_FALLBACK


_EXTENDED_ASK_USER_TOOL = StructuredTool.from_function(
    func=_extended_ask_fallback,
    name=ASK_USER_TOOL_NAME,
    description=_EXTENDED_DESCRIPTION,
    args_schema=ExtendedAskUserQuestionArgs,
)


def _enhance_request(request: ModelRequest[Any]) -> ModelRequest[Any]:
    tools_in = list(request.tools or [])
    if not tools_in:
        return request

    tools_out: list[Any] = []
    tools_changed = False

    for tool in tools_in:
        if isinstance(tool, BaseTool) and tool.name == ASK_USER_TOOL_NAME:
            tools_out.append(_EXTENDED_ASK_USER_TOOL)
            tools_changed = True
        else:
            tools_out.append(tool)

    if not tools_changed:
        return request
    return request.override(tools=tools_out)


def _headless_fallback_if_ask(request: ToolCallRequest) -> ToolMessage | None:
    tool_call = request.tool_call
    if str(tool_call.get("name") or "") != ASK_USER_TOOL_NAME:
        return None
    # Reached only when no interactive HITL channel intercepted ask_user_question;
    # bypass the compiled harness tool's 4-option Pydantic cap so headless runs stay smooth.
    return ToolMessage(
        content=HEADLESS_FALLBACK,
        name=ASK_USER_TOOL_NAME,
        tool_call_id=str(tool_call.get("id") or ""),
    )


class AskUserOptionsMiddleware(AgentMiddleware[Any, Any]):
    """Allow up to 20 choices in ``ask_user_question``."""

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        return handler(_enhance_request(request))

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        return await handler(_enhance_request(request))

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        fallback = _headless_fallback_if_ask(request)
        if fallback is not None:
            return fallback
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        fallback = _headless_fallback_if_ask(request)
        if fallback is not None:
            return fallback
        return await handler(request)


__all__ = [
    "MAX_OPTIONS",
    "MAX_QUESTIONS",
    "AskUserOptionsMiddleware",
    "ExtendedAskOption",
    "ExtendedAskQuestion",
    "ExtendedAskUserQuestionArgs",
]
