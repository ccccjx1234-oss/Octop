"""MCN (经贝) connector — business tools backed by the employee's own token.

Each connector instance stores the MCN gateway URL and the business token of
the employee who installed it.  MCN keeps enforcing company/employee/role
permissions; the connector never falls back to a shared service account.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from octop.infra.agents.plugins.plugin_tool_names import sanitize_plugin_tool_name
from octop.infra.auth.youli.delivery_export import (
    export_delivery_result,
    get_download_task,
    list_delivery_projects,
    list_export_templates,
)
from octop.infra.auth.youli.mcn_client import McnClient, McnClientError

from .auth import _turn_context, build_mcn_client
from .common import _SAVED_QUOTES, _recall, _remember
from .crm_write import (
    _create_contract,
    _create_coupon_version,
    _create_project,
    _create_sub_project,
    _resolve_protect_type,
)
from .lookup import (
    _get_contract,
    _get_quote,
    _list_accounts,
    _list_company_entities,
    _list_contracts,
    _list_coupon_versions,
    _list_customer_ownerships,
    _list_customers,
    _list_dictionaries,
    _list_opportunities,
    _list_price_versions,
    _list_products,
    _list_projects,
    _list_quotes,
    _list_service_items,
    _query_account_specs,
)
from .order import (
    _create_combined_order,
    _create_order_from_quote,
    _resolve_sub_project_for_order,
    _update_publish_link,
)
from .quote import _create_package_quote, _find_opportunity, _parse_quote, _save_quote
from .schemas import _WRITE_TOOLS, TOOLS

# The legacy youli tool entry forwards here so quote/order writes keep one
# implementation. Both surfaces must stay in step; do not fork these again.
save_quote = _save_quote
create_package_quote = _create_package_quote
create_order_from_quote = _create_order_from_quote
create_combined_order = _create_combined_order

_HandlerFn = Callable[[McnClient, dict[str, Any]], str]

_HANDLERS: dict[str, tuple[str, _HandlerFn]] = {
    "youli_list_accounts": ("查询账号", _list_accounts),
    "youli_query_account_specs": ("查询账号规格", _query_account_specs),
    "youli_list_quotes": ("查询报价单", _list_quotes),
    "youli_get_quote": ("查询报价单详情", _get_quote),
    "youli_list_products": ("查询产品", _list_products),
    "youli_list_customers": ("查询客户", _list_customers),
    "youli_list_customer_ownerships": ("查询客户归属", _list_customer_ownerships),
    "youli_list_contracts": ("查询合同", _list_contracts),
    "youli_get_contract": ("查询合同详情", _get_contract),
    "youli_list_company_entities": ("查询签约主体", _list_company_entities),
    "youli_list_opportunities": ("查询客户商机", _list_opportunities),
    "youli_list_dictionaries": ("查询字典", _list_dictionaries),
    "youli_list_projects": ("查询项目", _list_projects),
    "youli_list_price_versions": ("查询价格版本", _list_price_versions),
    "youli_list_coupon_versions": ("查询优惠政策版本", _list_coupon_versions),
    "youli_parse_quote": ("解析报价", _parse_quote),
    "youli_save_quote": ("保存报价单", _save_quote),
    "youli_create_package_quote": ("创建拉包报价", _create_package_quote),
    "youli_create_order_from_quote": ("下单", _create_order_from_quote),
    "youli_create_combined_order": ("组合下单", _create_combined_order),
    "youli_list_service_items": ("查询服务项目", _list_service_items),
    "youli_update_publish_link": ("填写发布链接", _update_publish_link),
    "youli_list_delivery_projects": ("查询交付项目", list_delivery_projects),
    "youli_list_export_templates": ("查询导出模板", list_export_templates),
    "youli_get_download_task": ("查询导出文件", get_download_task),
    "youli_export_delivery": ("导出交付数据", export_delivery_result),
    "youli_create_project": ("创建项目", _create_project),
    "youli_create_sub_project": ("创建项目", _create_sub_project),
    "youli_create_coupon_version": ("创建优惠政策版本", _create_coupon_version),
    "youli_create_contract": ("创建合同", _create_contract),
}


def _client(creds: dict[str, Any]) -> McnClient:
    return build_mcn_client(creds, turn_context_fn=_turn_context)


def is_write_tool_name(tool_name: str) -> bool:
    """Return True if *tool_name* refers to one of the MCN write tools."""
    return any(
        tool_name == raw_name or tool_name.endswith(f"_{raw_name}") for raw_name in _WRITE_TOOLS
    )


def write_tool_names(mcp_server_name: str) -> list[str]:
    """Return runtime tool names in TOOLS order, matching langchain registration."""
    used: set[str] = set()
    names: list[str] = []
    for tool in TOOLS:
        raw_name = str(tool.get("name") or "")
        safe_name = sanitize_plugin_tool_name(f"{mcp_server_name}_{raw_name}", used=used)
        if raw_name in _WRITE_TOOLS:
            names.append(safe_name)
    return names


def _confirm_when(req: Any) -> bool:
    tool_call = getattr(req, "tool_call", None)
    if tool_call is None and isinstance(req, dict):
        tool_call = req.get("tool_call")
    raw_args = tool_call.get("args") if isinstance(tool_call, dict) else None
    if not isinstance(raw_args, dict):
        return False
    val = raw_args.get("confirm")
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in {"true", "1", "yes"}
    if isinstance(val, (int, float)):
        return bool(val)
    return False


def write_interrupt_on(mcp_server_names: list[str]) -> dict[str, Any]:
    """Trigger interactive HITL confirmation whenever a MCN write tool sets confirm=true."""
    entry: dict[str, Any] = {"allowed_decisions": ["approve", "reject"], "when": _confirm_when}
    return {name: dict(entry) for server in mcp_server_names for name in write_tool_names(server)}


def list_tools() -> list[dict[str, Any]]:
    return TOOLS


def call_tool(creds: dict[str, Any], name: str, args: dict[str, Any]) -> str:
    handler = _HANDLERS.get(name)
    if handler is None:
        raise ValueError(f"unknown tool: {name}")
    label, fn = handler
    try:
        return fn(_client(creds), args or {})
    except McnClientError as exc:
        return f"{label}失败：{exc}"


def probe_credentials(creds: dict[str, Any]) -> None:
    client = _client(creds)
    client.post("/api/mcn/account/list", {"page": {"pageNum": 1, "pageSize": 1}})


__all__ = [
    "TOOLS",
    "McnClient",
    "McnClientError",
    "_HANDLERS",
    "_SAVED_QUOTES",
    "_client",
    "_confirm_when",
    "_find_opportunity",
    "_recall",
    "_remember",
    "_resolve_protect_type",
    "_resolve_sub_project_for_order",
    "_turn_context",
    "call_tool",
    "create_combined_order",
    "create_order_from_quote",
    "create_package_quote",
    "is_write_tool_name",
    "list_tools",
    "probe_credentials",
    "save_quote",
    "write_interrupt_on",
    "write_tool_names",
]
