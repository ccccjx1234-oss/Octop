"""LangChain tools that expose MCN business data to AIwork agents."""

from __future__ import annotations

import json
import hashlib
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Annotated, Any

from langchain_core.tools import StructuredTool
from langgraph.config import get_config
from pydantic import Field

from octop.config import YouliConfig
from octop.infra.auth.sso.crypto import decrypt_secret
from octop.infra.auth.youli.context import current_youli_sid, sid_for_thread
from octop.infra.auth.youli.mcn_client import McnClient, McnClientError

_MAX_OUTPUT_CHARS = 8000
_SAVED_QUOTES: dict[str, tuple[float, str]] = {}


def _money(value: Any) -> Decimal:
    return Decimal(str(value if value not in (None, "") else "0")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def _money_number(value: Decimal) -> float:
    return float(value)


def _line_quantity(spec: dict[str, Any]) -> int:
    raw = spec.get("quantity")
    if raw in (None, ""):
        raw = spec.get("count") if spec.get("count") not in (None, "") else 1
    quantity = int(raw)
    if quantity < 1 or quantity > 9999:
        raise ValueError("quantity")
    return quantity


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


def _order_item(item: dict[str, Any]) -> dict[str, Any]:
    """Map a quote-detail item into the order snapshot item shape."""
    return {
        # A fresh id per order: reusing the quote item id makes the amount
        # recalculation share service items across orders and divide amounts.
        "itemId": None,
        "type": item.get("type"),
        "skuId": item.get("skuId"),
        "accountId": item.get("accountId"),
        "accountName": item.get("accountName"),
        "packageId": item.get("packageId"),
        "count": item.get("count"),
        "quantity": item.get("quantity"),
        "price": item.get("originalPrice"),
        "originalPrice": item.get("originalPrice"),
        "originalMoney": item.get("originalMoney"),
        "couponMoney": item.get("couponMoney"),
        "couponAfterMoney": item.get("baseMoney"),
        "factMoney": item.get("factMoney"),
        "modeOperation": item.get("modeOperation"),
        "noCalculate": item.get("noCalculate"),
        "supplierId": item.get("supplierId"),
        "isAuditExempt": item.get("isAuditExempt"),
        "noteDescription": item.get("noteDescription"),
    }


def _item_supplier(item: dict[str, Any]) -> str | None:
    """Read supplier id from the item field or its dynamic JSON."""
    supplier = item.get("supplierId")
    if supplier not in (None, ""):
        return str(supplier)
    raw = item.get("dynamicItemObj")
    if not raw:
        return None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        value = (parsed or {}).get("formLabelAlign", {}).get("supplier")
        return str(value) if value not in (None, "") else None
    except (ValueError, AttributeError):
        return None


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

    def _cache_key(raw: str) -> str:
        return f"{_client().token_fingerprint}:{raw}"

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
        if not confirm:
            return "未保存：请先把报价单内容给用户核对，用户明确确认后再以 confirm=true 调用。"
        try:
            digest = hashlib.sha256(
                json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            cached = _SAVED_QUOTES.get(_cache_key(digest))
            if cached and time.time() - cached[0] < 600:
                return f"重复提交已忽略，上次结果：{cached[1]}"
            result = _json(_client().post("/api/mcn/offerOrder/save", payload))
            _SAVED_QUOTES[_cache_key(digest)] = (time.time(), result)
            return result
        except McnClientError as exc:
            return f"保存报价单失败：{exc}"

    async def create_package_quote(
        name: Annotated[str, Field(description="报价单名称")],
        product_id: Annotated[str, Field(description="产品 ID")],
        customer_id: Annotated[str, Field(description="客户 ID")],
        price_version_id: Annotated[str, Field(description="价格版本 ID")],
        coupon_version_id: Annotated[str, Field(description="优惠政策版本 ID")],
        account_specs: Annotated[
            list[dict[str, Any]],
            Field(
                description=(
                    "账号规格列表，每项包含 account_id、account_name、sku_id、price、"
                    "mode_operation（可选，默认 1）"
                )
            ),
        ],
        confirm: Annotated[
            bool, Field(description="用户核对账号、规格、价格并明确确认后才传 true")
        ] = False,
    ) -> str:
        """创建拉包报价单：先选账号，再按账号+规格+价格逐行保存。"""
        if not confirm:
            return "未保存：请先把账号、规格、价格和合计给用户核对，确认后再以 confirm=true 调用。"
        if not account_specs:
            return "未保存：至少选择一个账号规格。"
        try:
            items: list[dict[str, Any]] = []
            total = Decimal("0.00")
            for spec in account_specs:
                try:
                    quantity = _line_quantity(spec)
                    unit = _money(spec.get("price"))
                except (InvalidOperation, ValueError, ArithmeticError):
                    return "未保存：账号规格的价格或数量不正确。"
                line = (unit * quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                total += line
                unit_number = _money_number(unit)
                line_number = _money_number(line)
                items.append(
                    {
                        "type": 3,
                        "modeOperation": int(spec.get("mode_operation") or 1),
                        "skuId": str(spec.get("sku_id")),
                        "accountId": str(spec.get("account_id")),
                        "accountName": spec.get("account_name") or "",
                        "quantity": quantity,
                        "price": unit_number,
                        "originalPrice": unit_number,
                        "originalMoney": line_number,
                        "couponAfterMoney": line_number,
                        "factMoney": line_number,
                        "noCalculate": 0,
                    }
                )
            payload = {
                "offerOrderName": name,
                "productId": str(product_id),
                "customerId": str(customer_id),
                "priceVersion": str(price_version_id),
                "couponVersion": str(coupon_version_id),
                "orderMoney": _money_number(total),
                "factOrderMoney": _money_number(total),
                "itemRoList": items,
            }
            digest = hashlib.sha256(
                json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            cached = _SAVED_QUOTES.get(_cache_key(digest))
            if cached and time.time() - cached[0] < 600:
                return f"重复提交已忽略，上次结果：{cached[1]}"
            client = _client()
            saved = client.post("/api/mcn/offerOrder/save", payload)
            quotes = client.post(
                "/api/mcn/offerOrder/list",
                {
                    "page": {"pageNum": 1, "pageSize": 5},
                    "offerOrderName": name,
                },
            )
            result = _json(
                {
                    "saved": saved,
                    "quotes": _slim(
                        quotes,
                        (
                            "id",
                            "offerOrderNo",
                            "offerOrderName",
                            "customerName",
                            "orderMoney",
                            "factOrderMoney",
                            "status",
                        ),
                    ),
                }
            )
            _SAVED_QUOTES[_cache_key(digest)] = (time.time(), result)
            return result
        except McnClientError as exc:
            return f"创建拉包报价失败：{exc}"

    async def create_order_from_quote(
        quote_id: Annotated[str, Field(description="报价单 ID")],
        confirm: Annotated[
            bool, Field(description="用户核对报价和金额并明确确认后才传 true")
        ] = False,
    ) -> str:
        """从报价单创建销售订单：保存并提交，服务项目按报价明细自动拆出。"""
        if not confirm:
            return "未下单：请先把报价单号、客户和金额给用户核对，用户确认后再以 confirm=true 调用。"
        try:
            client = _client()
            cache_key = f"{client.token_fingerprint}:order:{quote_id}"
            cached = _SAVED_QUOTES.get(cache_key)
            if cached and time.time() - cached[0] < 600:
                return f"重复提交已忽略，上次结果：{cached[1]}"
            detail = client.get("/api/mcn/offerOrder/detail", {"id": str(quote_id)})
            if not isinstance(detail, dict):
                return "下单失败：报价单详情格式不正确"
            items = detail.get("offerOrderItemVos") or []
            if not items:
                return "下单失败：报价单没有明细"
            payload = {
                "submitMethod": 2,
                "offerOrderId": str(quote_id),
                "orderRo": {
                    "customerId": str(detail.get("customerId")),
                    "customerOpportunityId": detail.get("customerOpportunityId"),
                    "contractId": detail.get("contractId"),
                    "productId": str(detail.get("productId")),
                    "offerOrderId": str(quote_id),
                    "orderType": detail.get("orderType") or 1,
                    "commitType": 1,
                    "priceVersionId": str(detail.get("priceVersion")),
                    "couponVersionId": str(detail.get("couponVersion")),
                    "projectType": detail.get("projectType"),
                },
                "offerOrderSaveRo": {
                    "id": str(quote_id),
                    "offerOrderName": detail.get("offerOrderName"),
                    "productId": str(detail.get("productId")),
                    "customerId": str(detail.get("customerId")),
                    "priceVersion": str(detail.get("priceVersion")),
                    "couponVersion": str(detail.get("couponVersion")),
                    "orderMoney": detail.get("orderMoney"),
                    "factOrderMoney": detail.get("factOrderMoney"),
                    "orderType": detail.get("orderType") or 1,
                    "itemRoList": [_order_item(item) for item in items],
                },
                "orderExtraChargeRoList": [],
            }
            result = client.post("/api/mcn/order/temporaryStorage", payload)
            text_result = _json(
                {
                    "order": result,
                    "quoteId": str(quote_id),
                    "quoteNo": detail.get("offerOrderNo"),
                    "customerId": str(detail.get("customerId")),
                    "amount": detail.get("factOrderMoney"),
                }
            )
            _SAVED_QUOTES[cache_key] = (time.time(), text_result)
            return text_result
        except McnClientError as exc:
            return f"下单失败：{exc}"

    async def create_combined_order(
        quote_id: Annotated[str, Field(description="报价单 ID")],
        confirm: Annotated[
            bool, Field(description="用户核对拆单结果并明确确认后才传 true")
        ] = False,
    ) -> str:
        """组合订单：把一张拉包报价按供应商拆成多张订单，返回每张订单。"""
        if not confirm:
            return "未下单：请先说明将按供应商拆成几张订单及金额，用户确认后再以 confirm=true 调用。"
        try:
            client = _client()
            cache_key = f"{client.token_fingerprint}:combined:{quote_id}"
            cached = _SAVED_QUOTES.get(cache_key)
            if cached and time.time() - cached[0] < 600:
                return f"重复提交已忽略，上次结果：{cached[1]}"
            detail = client.get("/api/mcn/offerOrder/detail", {"id": str(quote_id)})
            if not isinstance(detail, dict):
                return "下单失败：报价单详情格式不正确"
            items = detail.get("offerOrderItemVos") or []
            if not items:
                return "下单失败：报价单没有明细"
            groups: dict[str, list[dict[str, Any]]] = {}
            for item in items:
                key = _item_supplier(item) or "unassigned"
                groups.setdefault(key, []).append(item)
            orders: list[dict[str, Any]] = []
            for supplier, group in groups.items():
                mapped_items = []
                amount = 0.0
                for item in group:
                    mapped = _order_item(item)
                    mapped_items.append(mapped)
                    amount += float(item.get("factMoney") or 0)
                payload = {
                    "submitMethod": 2,
                    "orderRo": {
                        "customerId": str(detail.get("customerId")),
                        "customerOpportunityId": detail.get("customerOpportunityId"),
                        "contractId": detail.get("contractId"),
                        "productId": str(detail.get("productId")),
                        "orderType": detail.get("orderType") or 1,
                        "commitType": 1,
                        "priceVersionId": str(detail.get("priceVersion")),
                        "couponVersionId": str(detail.get("couponVersion")),
                        "projectType": detail.get("projectType"),
                    },
                    "offerOrderSaveRo": {
                        "offerOrderName": detail.get("offerOrderName"),
                        "productId": str(detail.get("productId")),
                        "customerId": str(detail.get("customerId")),
                        "priceVersion": str(detail.get("priceVersion")),
                        "couponVersion": str(detail.get("couponVersion")),
                        "orderMoney": amount,
                        "factOrderMoney": amount,
                        "orderType": detail.get("orderType") or 1,
                        "itemRoList": mapped_items,
                    },
                    "orderExtraChargeRoList": [],
                }
                result = client.post("/api/mcn/order/temporaryStorage", payload)
                orders.append(
                    {"supplier": supplier, "amount": amount, "result": result}
                )
            text_result = _json(
                {
                    "quoteId": str(quote_id),
                    "quoteNo": detail.get("offerOrderNo"),
                    "groups": len(orders),
                    "orders": orders,
                }
            )
            _SAVED_QUOTES[cache_key] = (time.time(), text_result)
            return text_result
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
        export_template_id: Annotated[str, Field(description="客户导出模板 ID，可空")] = "",
        confirm: Annotated[
            bool, Field(description="用户核对导出范围后确认才传 true")
        ] = False,
    ) -> str:
        """下载交付数据：按客户模板创建项目或链接维度的导出任务。"""
        if not confirm:
            return "未导出：请先和用户确认导出维度和范围，确认后再以 confirm=true 调用。"
        if mode not in {"project", "link"}:
            return "导出失败：mode 只能是 project 或 link。"
        try:
            payload: dict[str, Any] = {}
            if project_name.strip():
                payload["projectName"] = project_name.strip()
            if str(customer_opportunity_id).strip():
                payload["customerOpportunityIds"] = [str(customer_opportunity_id)]
            if str(export_template_id).strip():
                payload["exportTemplateId"] = str(export_template_id)
            path = (
                "/api/mcn/deliveryDashboard/project/export"
                if mode == "project"
                else "/api/mcn/deliveryDashboard/link/export"
            )
            result = _client().post(path, payload)
            return _json(
                {
                    "mode": mode,
                    "result": result,
                    "note": "导出任务已创建，请在交付看板的下载中心获取文件。",
                }
            )
        except McnClientError as exc:
            return f"导出交付数据失败：{exc}"

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
            description="创建拉包报价单：按账号+规格+价格保存（写操作，需用户确认）。",
        ),
        StructuredTool.from_function(
            coroutine=create_order_from_quote,
            name="youli_create_order_from_quote",
            description="从报价单创建并提交销售订单（写操作，需用户确认）。",
        ),
        StructuredTool.from_function(
            coroutine=create_combined_order,
            name="youli_create_combined_order",
            description="组合订单：按供应商把一张报价拆成多张订单（写操作，需用户确认）。",
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
            description="按项目或链接创建交付数据导出任务（写操作，需用户确认）。",
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
