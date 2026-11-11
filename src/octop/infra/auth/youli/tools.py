"""LangChain tools that expose MCN business data to AIwork agents."""

from __future__ import annotations

import json
from typing import Annotated, Any, Protocol, cast

from langchain_core.tools import StructuredTool
from langgraph.config import get_config
from pydantic import Field

from octop.config import YouliConfig
from octop.infra.auth.sso.crypto import decrypt_secret
from octop.infra.auth.youli.context import current_youli_sid, sid_for_thread
from octop.infra.auth.youli.delivery_export import (
    EXPORT_DESCRIPTION,
    GET_DOWNLOAD_TASK_DESCRIPTION,
    LIST_DELIVERY_PROJECTS_DESCRIPTION,
    LIST_EXPORT_TEMPLATES_DESCRIPTION,
    export_delivery_result,
    get_download_task,
)
from octop.infra.auth.youli.delivery_export import (
    list_delivery_projects as query_delivery_projects,
)
from octop.infra.auth.youli.delivery_export import (
    list_export_templates as query_export_templates,
)
from octop.infra.auth.youli.mcn_client import McnClient, McnClientError

_MAX_OUTPUT_CHARS = 8000


def _json(data: Any) -> str:
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return text[:_MAX_OUTPUT_CHARS] + "...(结果已截断)"


def _slim(data: Any, keys: tuple[str, ...]) -> Any:
    """Keep list responses small enough for the model to reason about."""
    if not isinstance(data, list):
        return data
    return [
        {key: item.get(key) for key in keys if key in item}
        for item in data
        if isinstance(item, dict)
    ]


def _current_sid() -> str | None:
    """Prefer the sid stamped into the run config; fall back to the request context."""
    try:
        configurable = get_config().get("configurable") or {}
    except Exception:
        configurable = {}
    sid = str(configurable.get("youli_sid") or "").strip()
    if sid:
        return sid
    thread_sid = sid_for_thread(str(configurable.get("thread_id") or ""))
    return thread_sid or current_youli_sid()


class _SharedWriteApi(Protocol):
    """写操作签名；实现只有一份，见 connectors/gateway/adapters/mcn.py。"""

    def save_quote(self, client: McnClient, args: dict[str, Any]) -> str: ...

    def create_package_quote(self, client: McnClient, args: dict[str, Any]) -> str: ...

    def create_order_from_quote(self, client: McnClient, args: dict[str, Any]) -> str: ...

    def create_combined_order(self, client: McnClient, args: dict[str, Any]) -> str: ...


def _shared_write_api() -> _SharedWriteApi:
    """Quote and order writes live in the connector adapter; this entry forwards.

    Imported lazily so the module graph stays acyclic and the legacy tool
    entry cannot drift into a second implementation of the same write.
    """
    from octop.infra.connectors.gateway.adapters import mcn as adapter

    return cast(_SharedWriteApi, adapter)


def build_youli_tools(*, repos: Any, cfg: YouliConfig) -> list[StructuredTool]:
    """Return MCN read tools; they refuse when the turn has no delegated sid."""

    def _client() -> McnClient:
        sid = _current_sid()
        if not sid:
            raise McnClientError("当前会话没有经贝员工授权，不能访问业务数据")
        session = repos.youli_session_repo.get_active(sid)
        if session is None:
            raise McnClientError("登录已失效，请重新从经贝进入")
        if session.token_blob is None:
            raise McnClientError("当前会话缺少业务凭证")
        token = decrypt_secret(repos.secret_repo, session.token_blob)
        client = McnClient(cfg, token)
        if not client.enabled:
            raise McnClientError("MCN 业务接口未配置")
        return client

    async def list_accounts(
        account_name: Annotated[
            str, Field(description="账号名称关键字，可空")
        ] = "",
        platform: Annotated[
            int, Field(description="媒体平台：1抖音 2快手 3小红书 4微博 5微信 6今日头条 7B站；0 表示不限", ge=0)
        ] = 0,
        page: Annotated[int, Field(description="页码，从 1 开始", ge=1)] = 1,
        page_size: Annotated[int, Field(description="每页数量，最大 20", ge=1, le=20)] = 20,
    ) -> str:
        """查询账号库。拉包报价先选账号时使用，返回账号、平台、供应商、粉丝数等。"""
        try:
            payload: dict[str, Any] = {
                "page": {"pageNum": page, "pageSize": min(page_size, 20)}
            }
            if account_name.strip():
                payload["accountName"] = account_name.strip()
            if platform:
                payload["platforms"] = [platform]
            data = _client().post("/api/mcn/account/list", payload)
            return _json(
                _slim(
                    data,
                    (
                        "id",
                        "accountNumber",
                        "accountName",
                        "platform",
                        "platformName",
                        "modeOperation",
                        "supplierId",
                        "fans",
                        "distributionGroupName",
                    ),
                )
            )
        except McnClientError as exc:
            return f"查询账号失败：{exc}"

    async def query_account_specs(
        product_id: Annotated[str, Field(description="产品 ID；账号可报价的产品从业务上下文获取")],
        account_ids: Annotated[
            list[str], Field(description="账号 ID 列表，最多 50 个，一次批量查询")
        ],
        version_id: Annotated[str, Field(description="价格版本 ID，可空表示默认版本")] = "",
    ) -> str:
        """批量查询所选账号的可用规格和价格。拉包报价第二步使用。"""
        try:
            ids = [str(item).strip() for item in account_ids if str(item).strip()]
            if not ids:
                return "查询规格失败：account_ids 不能为空"
            if len(ids) > 50:
                return "查询规格失败：一次最多 50 个账号"
            payload: dict[str, Any] = {
                "productId": str(product_id),
                "accountIds": ids,
            }
            if str(version_id).strip():
                payload["versionId"] = str(version_id)
            return _json(
                _client().post(
                    "/api/mcn/account/quotation/queryByProductSpecList", payload
                )
            )
        except McnClientError as exc:
            return f"查询账号规格失败：{exc}"

    async def list_quotes(
        keyword: Annotated[str, Field(description="报价单名称或编号关键字，可空")] = "",
        page: Annotated[int, Field(description="页码，从 1 开始", ge=1)] = 1,
        page_size: Annotated[int, Field(description="每页数量，最大 20", ge=1, le=20)] = 10,
    ) -> str:
        """查询报价单列表，返回报价单号、客户、金额和状态。"""
        try:
            payload: dict[str, Any] = {
                "page": {"pageNum": page, "pageSize": min(page_size, 20)}
            }
            if keyword.strip():
                payload["offerOrderName"] = keyword.strip()
            data = _client().post("/api/mcn/offer/order/list", payload)
            return _json(
                _slim(
                    data,
                    (
                        "id",
                        "offerOrderNo",
                        "offerOrderName",
                        "customerName",
                        "productName",
                        "orderMoney",
                        "factOrderMoney",
                        "status",
                        "createdTime",
                    ),
                )
            )
        except McnClientError as exc:
            return f"查询报价单失败：{exc}"

    async def get_quote(
        quote_id: Annotated[str, Field(description="报价单 ID")],
    ) -> str:
        """读取报价单详情，含明细、账号、规格、数量和金额。"""
        try:
            return _json(
                _client().get("/api/mcn/offer/order/detail", {"id": str(quote_id)})
            )
        except McnClientError as exc:
            return f"查询报价单详情失败：{exc}"

    async def list_products(
        keyword: Annotated[str, Field(description="产品名称关键字，可空")] = "",
    ) -> str:
        """查询产品列表，用于确定报价单的 productId。"""
        try:
            payload: dict[str, Any] = {"page": {"pageNum": 1, "pageSize": 20}}
            if keyword.strip():
                payload["name"] = keyword.strip()
            data = _client().post("/api/mcn/product/list", payload)
            return _json(
                _slim(
                    data,
                    ("id", "name", "productType", "attribute", "productCategoryId"),
                )
            )
        except McnClientError as exc:
            return f"查询产品失败：{exc}"

    async def list_customers(
        keyword: Annotated[str, Field(description="客户名称关键字，可空")] = "",
    ) -> str:
        """查询客户列表，用于确定客户和商机。"""
        try:
            payload: dict[str, Any] = {"page": {"pageNum": 1, "pageSize": 20}}
            if keyword.strip():
                payload["name"] = keyword.strip()
            data = _client().post("/api/mcn/customer-management/list", payload)
            return _json(
                _slim(data, ("id", "name", "category", "contact", "mobile"))
            )
        except McnClientError as exc:
            return f"查询客户失败：{exc}"

    async def list_contracts(
        keyword: Annotated[str, Field(description="合同名称或编号关键字，可空")] = "",
        customer_id: Annotated[str, Field(description="客户 ID，可空")] = "",
    ) -> str:
        """查询销售合同，用于报价单或订单选择合同。"""
        try:
            payload: dict[str, Any] = {"page": {"pageNum": 1, "pageSize": 20}}
            if keyword.strip():
                payload["keyword"] = keyword.strip()
            if str(customer_id).strip():
                payload["customerId"] = str(customer_id)
            data = _client().post("/api/mcn/contract/list", payload)
            return _json(
                _slim(
                    data,
                    ("id", "name", "customerName", "status", "amount", "startTime", "endTime"),
                )
            )
        except McnClientError as exc:
            return f"查询合同失败：{exc}"

    async def list_price_versions(
        keyword: Annotated[str, Field(description="价格版本名称关键字，可空")] = "",
    ) -> str:
        """查询账号价格版本，报价时用于取价。"""
        try:
            payload: dict[str, Any] = {"page": {"pageNum": 1, "pageSize": 20}}
            if keyword.strip():
                payload["name"] = keyword.strip()
            data = _client().post("/api/mcn/account/quote/version/list", payload)
            return _json(
                _slim(data, ("id", "name", "version", "isDefault", "status"))
            )
        except McnClientError as exc:
            return f"查询价格版本失败：{exc}"

    async def list_coupon_versions(
        keyword: Annotated[str, Field(description="优惠政策版本名称关键字，可空")] = "",
    ) -> str:
        """查询优惠政策版本，报价时用于计算优惠。"""
        try:
            payload: dict[str, Any] = {"page": {"pageNum": 1, "pageSize": 20}}
            if keyword.strip():
                payload["name"] = keyword.strip()
            data = _client().post("/api/mcn/coupon/version/list", payload)
            return _json(
                _slim(data, ("id", "name", "version", "isDefault", "status"))
            )
        except McnClientError as exc:
            return f"查询优惠政策版本失败：{exc}"

    async def parse_quote(
        raw_text: Annotated[str, Field(description="客户需求原文，包含账号、规格、数量、价格等")],
        product_id: Annotated[str, Field(description="产品 ID")],
    ) -> str:
        """用 MCN 现有 AI 能力把报价描述解析成结构化报价单草稿。"""
        try:
            return _json(
                _client().post(
                    "/api/mcn/offerOrder/aiParse",
                    {"rawText": raw_text, "productId": str(product_id)},
                )
            )
        except McnClientError as exc:
            return f"解析报价失败：{exc}"

    async def save_quote(
        payload: Annotated[dict[str, Any], Field(description="OfferOrderSaveRo 结构的报价单")],
        confirm: Annotated[
            bool, Field(description="用户核对报价内容并明确确认后才传 true")
        ] = False,
    ) -> str:
        """保存报价单。写操作，必须先用 confirm=false 展示内容，用户确认后再保存。"""
        try:
            return _shared_write_api().save_quote(
                _client(), {"payload": payload, "confirm": confirm}
            )
        except McnClientError as exc:
            return f"保存报价单失败：{exc}"

    async def create_package_quote(
        name: Annotated[str, Field(description="报价单名称")],
        product_id: Annotated[str, Field(description="产品 ID")],
        customer_id: Annotated[str, Field(description="客户 ID")],
        account_specs: Annotated[
            list[dict[str, Any]],
            Field(
                description=(
                    "账号规格列表，每项包含 account_id、sku_id；price 可省略以取规格价格，"
                    "quantity 默认 1。账号名、规格名、平台、运营方式自动查询"
                )
            ),
        ],
        price_version_id: Annotated[
            str, Field(description="用户指定时传；省略则按合同保价或默认版本自动选择")
        ] = "",
        coupon_version_id: Annotated[
            str, Field(description="用户指定时传；省略则按合同保价或默认版本自动选择")
        ] = "",
        project_type: Annotated[
            int | None, Field(description="散投自动带出；项目产品需明确业务类型")
        ] = None,
        order_type: Annotated[int, Field(description="订单类型，默认常规单 1")] = 1,
        contract_id: Annotated[
            str, Field(description="可空，默认商机下首个有效已审核合同")
        ] = "",
        customer_opportunity_id: Annotated[
            str, Field(description="客户商机 ID，可空；传了报价和后续订单才能归到项目")
        ] = "",
        expected_amount: Annotated[
            float | None, Field(description="用户已确认的草稿应付金额；金额变化会阻止保存")
        ] = None,
        confirm: Annotated[bool, Field(description="用户确认后才传 true")] = False,
    ) -> str:
        """创建账号报价单：与连接器工具共用同一份实现。"""
        try:
            return _shared_write_api().create_package_quote(
                _client(),
                {
                    "name": name,
                    "product_id": product_id,
                    "customer_id": customer_id,
                    "price_version_id": price_version_id,
                    "coupon_version_id": coupon_version_id,
                    "project_type": project_type,
                    "order_type": order_type,
                    "contract_id": contract_id,
                    "customer_opportunity_id": customer_opportunity_id,
                    "expected_amount": expected_amount,
                    "account_specs": account_specs,
                    "confirm": confirm,
                },
            )
        except McnClientError as exc:
            return f"创建拉包报价失败：{exc}"

    async def create_order_from_quote(
        quote_id: Annotated[str, Field(description="报价单 ID")],
        project_id: Annotated[str, Field(description="父项目 ID，可空")] = "",
        sub_project_id: Annotated[str, Field(description="子项目 ID，可空")] = "",
        confirm: Annotated[bool, Field(description="用户确认后才传 true")] = False,
    ) -> str:
        """从报价单创建销售订单：与连接器工具共用同一份实现。"""
        try:
            return _shared_write_api().create_order_from_quote(
                _client(),
                {
                    "quote_id": quote_id,
                    "project_id": project_id,
                    "sub_project_id": sub_project_id,
                    "confirm": confirm,
                },
            )
        except McnClientError as exc:
            return f"下单失败：{exc}"

    async def create_combined_order(
        quote_id: Annotated[str, Field(description="报价单 ID")],
        confirm: Annotated[bool, Field(description="用户确认后才传 true")] = False,
    ) -> str:
        """组合订单：按供应商拆单，与连接器工具共用同一份实现。"""
        try:
            return _shared_write_api().create_combined_order(
                _client(), {"quote_id": quote_id, "confirm": confirm}
            )
        except McnClientError as exc:
            return f"组合下单失败：{exc}"

    async def list_service_items(
        order_id: Annotated[str, Field(description="销售订单 ID")],
        page: Annotated[int, Field(description="页码", ge=1)] = 1,
        page_size: Annotated[int, Field(description="每页数量，最大 20", ge=1, le=20)] = 20,
    ) -> str:
        """查询订单的服务项目，用于按账号+规格填写发布链接。"""
        try:
            data = _client().post(
                "/api/mcn/order/service/item/page/list",
                {
                    "page": {"pageNum": page, "pageSize": min(page_size, 20)},
                    "orderId": str(order_id),
                },
            )
            return _json(
                _slim(
                    data,
                    (
                        "id",
                        "orderId",
                        "accountId",
                        "accountName",
                        "accountSpecId",
                        "publishLink",
                        "publishTime",
                        "flowStatus",
                        "orderAmount",
                    ),
                )
            )
        except McnClientError as exc:
            return f"查询服务项目失败：{exc}"

    async def update_publish_link(
        service_item_id: Annotated[str, Field(description="服务项目 ID")],
        publish_link: Annotated[str, Field(description="发布链接")],
        publish_time: Annotated[str, Field(description="发布时间，可空，格式 yyyy-MM-dd HH:mm:ss")] = "",
        confirm: Annotated[
            bool, Field(description="用户核对账号、规格和链接后确认才传 true")
        ] = False,
    ) -> str:
        """填写服务项目的发布链接和发布时间（写操作，需用户确认）。"""
        link = publish_link.strip()
        if not link:
            return "未保存：发布链接不能为空。"
        if not confirm:
            return "未保存：请先让用户核对服务项目、账号规格和链接，确认后再以 confirm=true 调用。"
        try:
            payload: dict[str, Any] = {
                "id": str(service_item_id),
                "publishLink": link,
            }
            if publish_time.strip():
                payload["publishTime"] = publish_time.strip()
            return _json(
                _client().post("/api/mcn/order/service/item/edit", payload)
            )
        except McnClientError as exc:
            return f"填写发布链接失败：{exc}"

    async def export_delivery(
        mode: Annotated[str, Field(description="project 按项目导出，link 按链接导出")] = "project",
        project_name: Annotated[str, Field(description="项目名称关键字，可空")] = "",
        customer_opportunity_id: Annotated[str, Field(description="客户商机 ID，可空")] = "",
        export_template_id: Annotated[
            str,
            Field(description="仅按链接导出且用户指定模板名称时传入，不要问用户要 ID"),
        ] = "",
        confirm: Annotated[
            bool, Field(description="用户用业务语言同意导出范围后才传 true，不要向用户提这个参数")
        ] = False,
    ) -> str:
        """按用户确认过的项目名和导出方式创建交付导出，并取回下载链接。"""
        try:
            return export_delivery_result(
                _client(),
                {
                    "mode": mode,
                    "project_name": project_name,
                    "customer_opportunity_id": customer_opportunity_id,
                    "export_template_id": export_template_id,
                    "confirm": confirm,
                },
            )
        except McnClientError as exc:
            return f"导出交付数据失败：{exc}"

    async def list_delivery_projects(
        keyword: Annotated[str, Field(description="用户说的或截图上的项目名，可空")] = "",
        name_field: Annotated[
            str, Field(description="project 查娱乐/品牌名，brand 查订单项目名")
        ] = "project",
    ) -> str:
        """按名称核对交付看板中的项目。"""
        try:
            return query_delivery_projects(
                _client(), {"keyword": keyword, "name_field": name_field}
            )
        except McnClientError as exc:
            return f"查询交付项目失败：{exc}"

    async def list_export_templates(
        template_name: Annotated[str, Field(description="模板名称，可空")] = "",
    ) -> str:
        """查询按链接导出可用的模板名称。"""
        try:
            return query_export_templates(_client(), {"template_name": template_name})
        except McnClientError as exc:
            return f"查询导出模板失败：{exc}"

    async def fetch_download_task(
        task_id: Annotated[str, Field(description="导出返回的任务 ID，可空")] = "",
        file_prefix: Annotated[str, Field(description="没有任务 ID 时使用导出返回的 file_prefix")] = "",
    ) -> str:
        """查询交付导出文件是否已经生成。"""
        try:
            return get_download_task(
                _client(), {"task_id": task_id, "file_prefix": file_prefix}
            )
        except McnClientError as exc:
            return f"查询导出文件失败：{exc}"

    async def create_project(
        project_name: Annotated[str, Field(description="项目名称")],
        confirm: Annotated[bool, Field(description="用户确认后才传 true")] = False,
    ) -> str:
        """创建客户项目（写操作，需用户确认）。"""
        name = project_name.strip()
        if not name:
            return "未创建：项目名称不能为空。"
        if not confirm:
            return "未创建：请先和用户确认项目名称，确认后再以 confirm=true 调用。"
        try:
            return _json(
                _client().post(
                    "/api/mcn/customerProject/addProject", {"projectName": name}
                )
            )
        except McnClientError as exc:
            return f"创建项目失败：{exc}"

    async def create_coupon_version(
        name: Annotated[str, Field(description="优惠政策版本名称")],
        version: Annotated[str, Field(description="版本号，可空")] = "",
        opportunity_id: Annotated[str, Field(description="客户商机 ID，可空；填了表示客户特定版本")] = "",
        is_default: Annotated[bool, Field(description="是否默认版本")] = False,
        effective_time: Annotated[str, Field(description="生效时间，可空")] = "",
        expiration_time: Annotated[str, Field(description="过期时间，可空")] = "",
        confirm: Annotated[bool, Field(description="用户确认后才传 true")] = False,
    ) -> str:
        """创建优惠政策版本，可指定客户商机做客户特定版本（写操作，需用户确认）。"""
        if not name.strip():
            return "未创建：版本名称不能为空。"
        if not confirm:
            return "未创建：请先和用户确认版本名称、客户和有效期，确认后再以 confirm=true 调用。"
        try:
            payload: dict[str, Any] = {
                "name": name.strip(),
                "isDefault": 1 if is_default else 0,
            }
            if version.strip():
                payload["version"] = version.strip()
            if str(opportunity_id).strip():
                payload["opportunityId"] = str(opportunity_id)
            if effective_time.strip():
                payload["effectiveTime"] = effective_time.strip()
            if expiration_time.strip():
                payload["expirationTime"] = expiration_time.strip()
            return _json(
                _client().post("/api/mcn/coupon/version/addCouponVersion", payload)
            )
        except McnClientError as exc:
            return f"创建优惠政策版本失败：{exc}"

    async def create_contract(
        payload: Annotated[
            dict[str, Any],
            Field(description="ContractRo 结构，至少包含 enableType、name、customerId、type"),
        ],
        confirm: Annotated[bool, Field(description="用户确认后才传 true")] = False,
    ) -> str:
        """创建销售合同（写操作，需用户确认）。"""
        if not confirm:
            return "未创建：请先和用户核对合同类型、客户、保价政策和有效期，确认后再以 confirm=true 调用。"
        if not payload.get("enableType"):
            return "未创建：enableType 不能为空。"
        try:
            return _json(_client().post("/api/mcn/contract/addContract", payload))
        except McnClientError as exc:
            return f"创建合同失败：{exc}"

    return [
        StructuredTool.from_function(
            coroutine=list_accounts,
            name="youli_list_accounts",
            description="查询经贝 MCN 账号库，拉包报价先选账号时使用。",
        ),
        StructuredTool.from_function(
            coroutine=query_account_specs,
            name="youli_query_account_specs",
            description="批量查询所选账号的可用规格与价格，拉包报价第二步使用。",
        ),
        StructuredTool.from_function(
            coroutine=list_quotes,
            name="youli_list_quotes",
            description="查询经贝 MCN 报价单列表。",
        ),
        StructuredTool.from_function(
            coroutine=get_quote,
            name="youli_get_quote",
            description="读取经贝 MCN 报价单详情。",
        ),
        StructuredTool.from_function(
            coroutine=list_products,
            name="youli_list_products",
            description="查询经贝 MCN 产品列表。",
        ),
        StructuredTool.from_function(
            coroutine=list_customers,
            name="youli_list_customers",
            description="查询经贝 MCN 客户列表。",
        ),
        StructuredTool.from_function(
            coroutine=list_contracts,
            name="youli_list_contracts",
            description="查询经贝 MCN 销售合同。",
        ),
        StructuredTool.from_function(
            coroutine=list_price_versions,
            name="youli_list_price_versions",
            description="查询经贝 MCN 账号价格版本。",
        ),
        StructuredTool.from_function(
            coroutine=list_coupon_versions,
            name="youli_list_coupon_versions",
            description="查询经贝 MCN 优惠政策版本。",
        ),
        StructuredTool.from_function(
            coroutine=parse_quote,
            name="youli_parse_quote",
            description="把报价描述解析成结构化报价单草稿。",
        ),
        StructuredTool.from_function(
            coroutine=save_quote,
            name="youli_save_quote",
            description="保存经贝 MCN 报价单（写操作，需用户确认）。",
        ),
        StructuredTool.from_function(
            coroutine=create_package_quote,
            name="youli_create_package_quote",
            description=(
                "创建账号报价单：先用 confirm=false 生成只读草稿，自动带出散投项目类型、"
                "常规单、合同或默认价格/优惠版本并试算优惠。不要向用户索要 ID，不要逐个"
                "确认默认字段；汇总草稿金额一次确认后 confirm=true 保存。"
            ),
        ),
        StructuredTool.from_function(
            coroutine=create_order_from_quote,
            name="youli_create_order_from_quote",
            description="从报价单创建并提交销售订单，保留项目类型和商机（写操作，需用户确认）。",
        ),
        StructuredTool.from_function(
            coroutine=create_combined_order,
            name="youli_create_combined_order",
            description="组合订单：按供应商把一张报价拆成多张订单，保留项目类型和商机（写操作，需用户确认）。",
        ),
        StructuredTool.from_function(
            coroutine=list_service_items,
            name="youli_list_service_items",
            description="查询订单服务项目（账号+规格+发布链接）。",
        ),
        StructuredTool.from_function(
            coroutine=update_publish_link,
            name="youli_update_publish_link",
            description="填写服务项目发布链接（写操作，需用户确认）。",
        ),
        StructuredTool.from_function(
            coroutine=export_delivery,
            name="youli_export_delivery",
            description=EXPORT_DESCRIPTION,
        ),
        StructuredTool.from_function(
            coroutine=list_delivery_projects,
            name="youli_list_delivery_projects",
            description=LIST_DELIVERY_PROJECTS_DESCRIPTION,
        ),
        StructuredTool.from_function(
            coroutine=list_export_templates,
            name="youli_list_export_templates",
            description=LIST_EXPORT_TEMPLATES_DESCRIPTION,
        ),
        StructuredTool.from_function(
            coroutine=fetch_download_task,
            name="youli_get_download_task",
            description=GET_DOWNLOAD_TASK_DESCRIPTION,
        ),
        StructuredTool.from_function(
            coroutine=create_project,
            name="youli_create_project",
            description="创建客户项目（写操作，需用户确认）。",
        ),
        StructuredTool.from_function(
            coroutine=create_coupon_version,
            name="youli_create_coupon_version",
            description="创建优惠政策版本，含客户特定版本（写操作，需用户确认）。",
        ),
        StructuredTool.from_function(
            coroutine=create_contract,
            name="youli_create_contract",
            description="创建销售合同（写操作，需用户确认）。",
        ),
    ]
