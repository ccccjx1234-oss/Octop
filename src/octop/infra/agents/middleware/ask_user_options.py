"""Extend ``ask_user_question`` option capacity and guide MCN interactive confirmation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import (
    AgentMiddleware,
    HumanInTheLoopMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_core.tools.base import BaseTool
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from octop_harness.builtin.tools.ask_user import ASK_USER_TOOL_NAME, HEADLESS_FALLBACK
from pydantic import BaseModel, Field, field_validator

from octop.infra.agents.security.hitl_session import HitlSessionPolicyStore
from octop.infra.connectors.gateway.adapters.mcn import _confirm_when, is_write_tool_name

MAX_QUESTIONS = 6
MAX_OPTIONS = 20

_MCN_SYSTEM_MARKER = "【MCN 主数据选择与单据交互确认规则】"
_MCN_SYSTEM_ADDENDUM = (
    "\n\n"
    f"{_MCN_SYSTEM_MARKER}\n"
    "1. **分页与总数检查**：调用 `youli_list_*` 查询客户、商机、合同、账号、产品、项目、"
    "价格/优惠版本、交付项目或导出模板时，必须查看返回的 `page` 字段"
    "（`pageNum`/`pageSize`/`totalCount`/`totalPage`/`hasMore`）；禁止把第 1 页当成全部数据，"
    "需要更多记录或搜索不同关键词时继续翻页或带关键字检索。\n"
    "2. **主数据多选（已扩展为最多 6 题、每题最多 20 个选项）**：当需要用户从客户、商机、合同、账号、规格、"
    "项目、版本或模板等主数据中拍板选择且候选超过 1 个时，优先调用 `ask_user_question` "
    "弹出选择卡片；单题可传入最多 20 个真实候选（`label` 简洁清晰，`description` 附关键上下文如"
    "编号/联系人/金额/类型；禁止臆造未查到的记录）。若 `hasMore=true`，在 `question` 中写明"
    "「共 X 条，当前第 Y 页」，并可附带「查看下一页」或提示点击「其他」输入关键词筛选。\n"
    "3. **必要字段缺失时必须持续引导到完整**：办理 MCN 写单据（尤其是创建报价单缺少客户、产品、"
    "投放账号 `account_specs`、规格或数量时）：\n"
    "   - **禁止**在必要字段仍缺失时仅发纯文本（如“请告诉我账号名字/等你的账号名单”）并结束本轮对话；\n"
    "   - **禁止**先问“你报名字还是我筛选给你看”这类元问题；缺少账号/客户/项目等主数据时，"
    "直接调用 `youli_list_*` 拉取真实候选（每题最多 20 条）放进 `ask_user_question` 供用户勾选（前端自带关键词过滤与「其他」自由输入），"
    "或需要自由填写名称/关键词时在 `ask_user_question` 中使用 `options=[]` 开放输入；\n"
    "   - 拿到账号后若规格/数量仍未定，必须紧接着调 `youli_query_account_specs` 并再次用 `ask_user_question` "
    "引导选齐规格与数量，直到写单据所需的全部必填字段齐备，直接进入试算与确认保存。\n"
    "4. **写单据弹出确认卡片**：对于所有 MCN 写单据（创建/保存报价、从报价下单、组合下单、"
    "填写发布链接、导出交付数据、创建项目/子项目、创建优惠政策版本、创建合同）：\n"
    "   - 业务字段存在多种选择或歧义时，先用 `ask_user_question` 一次性向用户问清；\n"
    "   - 报价（`youli_create_package_quote`）必须先调用 `confirm=false` 试算草稿金额并在消息中简要说明；\n"
    "   - 参数就绪准备提交写库时，调用写工具传入 `confirm=true`（并尽量在 `confirm_summary` "
    "填好客户/名称/关键数量与应付金额摘要），系统会自动弹出审批确认卡片由用户点击「批准」或「拒绝」，"
    "无需在此之前用 `ask_user_question` 重复询问“是否确认提交”。"
)

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


def _tool_name_of(tool: Any) -> str:
    if isinstance(tool, BaseTool):
        return str(tool.name or "")
    if isinstance(tool, dict):
        fn = tool.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            return str(fn["name"])
        return str(tool.get("name") or "")
    return str(getattr(tool, "name", "") or "")


def _is_mcn_tool(tool: Any) -> bool:
    name = _tool_name_of(tool)
    return name.startswith("youli_") or "_youli_" in name


def _with_mcn_guidance(system_message: SystemMessage | None) -> SystemMessage:
    if system_message is None:
        return SystemMessage(content=_MCN_SYSTEM_ADDENDUM.lstrip())
    content = system_message.content
    if isinstance(content, str):
        if _MCN_SYSTEM_MARKER in content:
            return system_message
        return SystemMessage(content=f"{content.rstrip()}{_MCN_SYSTEM_ADDENDUM}")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and _MCN_SYSTEM_MARKER in str(block.get("text") or ""):
                return system_message
        return SystemMessage(content=[*content, {"type": "text", "text": _MCN_SYSTEM_ADDENDUM}])
    return SystemMessage(content=f"{content}{_MCN_SYSTEM_ADDENDUM}")


def _enhance_request(request: ModelRequest[Any]) -> ModelRequest[Any]:
    tools_in = list(request.tools or [])
    if not tools_in:
        return request

    tools_out: list[Any] = []
    tools_changed = False
    has_mcn = False

    for tool in tools_in:
        if _is_mcn_tool(tool):
            has_mcn = True
        if isinstance(tool, BaseTool) and tool.name == ASK_USER_TOOL_NAME:
            tools_out.append(_EXTENDED_ASK_USER_TOOL)
            tools_changed = True
        else:
            tools_out.append(tool)

    overrides: dict[str, Any] = {}
    if tools_changed:
        overrides["tools"] = tools_out
    if has_mcn:
        updated_sys = _with_mcn_guidance(request.system_message)
        if updated_sys is not request.system_message:
            overrides["system_message"] = updated_sys

    if not overrides:
        return request
    return request.override(**overrides)


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
    """Allow up to 20 choices in ``ask_user_question`` and guide MCN flows."""

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


class _McnInterruptLookup:
    """Dynamic HITL rule lookup so MCN writes pause regardless of agent bootstrap/reload timing."""

    def __init__(self, session_store: HitlSessionPolicyStore | None) -> None:
        self._session_store = session_store

    def _when(self, req: Any, tool_name: str) -> bool:
        if self._session_store is not None and self._session_store.allows_interrupt(tool_name, req):
            return False
        return _confirm_when(req)

    def _rule_for(self, tool_name: str) -> dict[str, Any]:
        return {
            "allowed_decisions": ["approve", "reject"],
            "when": lambda req, _t=tool_name: self._when(req, _t),
        }

    def __contains__(self, tool_name: object) -> bool:
        return isinstance(tool_name, str) and is_write_tool_name(tool_name)

    def __getitem__(self, tool_name: str) -> dict[str, Any]:
        if not isinstance(tool_name, str) or not is_write_tool_name(tool_name):
            raise KeyError(tool_name)
        return self._rule_for(tool_name)

    def get(self, tool_name: Any, default: Any = None) -> Any:
        if not isinstance(tool_name, str) or not is_write_tool_name(tool_name):
            return default
        return self._rule_for(tool_name)


class McnWriteHitlMiddleware(HumanInTheLoopMiddleware):
    """Enforce interactive HITL approval on MCN write tools when ``confirm=true``."""

    def __init__(self, *, session_store: HitlSessionPolicyStore | None = None) -> None:
        super().__init__(interrupt_on={})
        self.interrupt_on = _McnInterruptLookup(session_store)  # type: ignore[assignment]


__all__ = [
    "MAX_OPTIONS",
    "MAX_QUESTIONS",
    "AskUserOptionsMiddleware",
    "ExtendedAskOption",
    "ExtendedAskQuestion",
    "ExtendedAskUserQuestionArgs",
    "McnWriteHitlMiddleware",
]
