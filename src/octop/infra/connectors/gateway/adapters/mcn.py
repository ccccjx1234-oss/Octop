"""MCN (经贝) connector — business tools backed by the employee's own token.

Each connector instance stores the MCN gateway URL and the business token of
the employee who installed it.  MCN keeps enforcing company/employee/role
permissions; the connector never falls back to a shared service account.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from octop.infra.auth.youli.delivery_export import (
    EXPORT_DESCRIPTION,
    GET_DOWNLOAD_TASK_DESCRIPTION,
    LIST_DELIVERY_PROJECTS_DESCRIPTION,
    LIST_EXPORT_TEMPLATES_DESCRIPTION,
    export_delivery_result,
    get_download_task,
    list_delivery_projects,
    list_export_templates,
)
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
    quantity = int(str(raw))
    if quantity < 1 or quantity > 9999:
        raise ValueError("quantity")
    return quantity


def _cache_scope(client: McnClient) -> str:
    fingerprint = getattr(client, "token_fingerprint", None) or getattr(client, "_token", None)
    if not fingerprint:
        fingerprint = "anonymous"
    return str(fingerprint)


def _scoped_cache_key(client: McnClient, key: str) -> str:
    return f"{_cache_scope(client)}:{key}"


def _recall(client: McnClient, key: str) -> str | None:
    cached = _SAVED_QUOTES.get(_scoped_cache_key(client, key))
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    return None


def _remember(client: McnClient, key: str, result: str) -> None:
    _SAVED_QUOTES[_scoped_cache_key(client, key)] = (time.time(), result)


def _turn_context() -> tuple[str, int | None]:
    try:
        from langgraph.config import get_config

        configurable = get_config().get("configurable") or {}
    except Exception:
        return "", None
    sid = str(configurable.get("youli_sid") or "").strip()
    raw_user = configurable.get("octop_user_id")
    if raw_user in (None, ""):
        return sid, None
    try:
        return sid, int(str(raw_user))
    except (TypeError, ValueError):
        return sid, None


def _owner_fallback_allowed(creds: dict[str, Any], sid: str, turn_user: int | None) -> bool:
    """The stored token belongs to the installer. Another employee must not use it."""
    owner = creds.get("_owner_user_id")
    if owner is None:
        return not sid
    if turn_user is not None and int(turn_user) != int(owner):
        return False
    return not (sid and turn_user is None)


def _json(data: Any) -> str:
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return text[:_MAX_OUTPUT_CHARS] + "...(结果已截断)"


def _slim(data: Any, keys: tuple[str, ...]) -> Any:
    if not isinstance(data, list):
        return data
    return [
        {key: item.get(key) for key in keys if key in item}
        for item in data
        if isinstance(item, dict)
    ]


def _order_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
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


def _client(creds: dict[str, Any]) -> McnClient:
    sid, turn_user = _turn_context()
    resolver = creds.get("_token_resolver")
    token = str(resolver() or "").strip() if callable(resolver) else ""
    if not token and _owner_fallback_allowed(creds, sid, turn_user):
        latest = creds.get("_latest_token_resolver")
        token = str(latest() or "").strip() if callable(latest) else ""
        if not token:
            # Background work for this same employee has no turn sid.
            token = str(creds.get("token") or "").strip()
    base_url = str(
        creds.get("base_url") or os.environ.get("OCTOP_YOULI_MCN_URL") or ""
    ).strip()
    if not token:
        raise McnClientError("MCN 连接器未授权，请重新登录会火员工账号")
    client = McnClient(None, token, base_url=base_url)
    if not client.enabled:
        raise McnClientError("MCN 业务接口未配置")
    return client


def _list_accounts(client: McnClient, args: dict[str, Any]) -> str:
    payload: dict[str, Any] = {
        "page": {
            "pageNum": max(1, int(args.get("page") or 1)),
            "pageSize": min(20, max(1, int(args.get("page_size") or 20))),
        }
    }
    name = str(args.get("account_name") or "").strip()
    if name:
        payload["accountName"] = name
    platform = int(args.get("platform") or 0)
    if platform:
        payload["platforms"] = [platform]
    data = client.post("/api/mcn/account/list", payload)
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


def _query_account_specs(client: McnClient, args: dict[str, Any]) -> str:
    ids = [str(item).strip() for item in (args.get("account_ids") or []) if str(item).strip()]
    if not ids:
        return "查询规格失败：account_ids 不能为空"
    if len(ids) > 50:
        return "查询规格失败：一次最多 50 个账号"
    payload: dict[str, Any] = {
        "productId": str(args.get("product_id") or ""),
        "accountIds": ids,
    }
    version_id = str(args.get("version_id") or "").strip()
    if version_id:
        payload["versionId"] = version_id
    return _json(
        client.post("/api/mcn/account/quotation/queryByProductSpecList", payload)
    )


def _list_quotes(client: McnClient, args: dict[str, Any]) -> str:
    payload: dict[str, Any] = {
        "page": {
            "pageNum": max(1, int(args.get("page") or 1)),
            "pageSize": min(20, max(1, int(args.get("page_size") or 10))),
        }
    }
    keyword = str(args.get("keyword") or "").strip()
    if keyword:
        payload["offerOrderName"] = keyword
    data = client.post("/api/mcn/offerOrder/list", payload)
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


def _get_quote(client: McnClient, args: dict[str, Any]) -> str:
    quote_id = str(args.get("quote_id") or "").strip()
    if not quote_id:
        return "查询报价单详情失败：quote_id 不能为空"
    return _json(client.get("/api/mcn/offerOrder/detail", {"id": quote_id}))


def _list_products(client: McnClient, args: dict[str, Any]) -> str:
    payload: dict[str, Any] = {"page": {"pageNum": 1, "pageSize": 20}}
    keyword = str(args.get("keyword") or "").strip()
    if keyword:
        payload["name"] = keyword
    data = client.post("/api/mcn/product/list", payload)
    return _json(_slim(data, ("id", "name", "productType", "attribute", "productCategoryId")))


def _list_customers(client: McnClient, args: dict[str, Any]) -> str:
    payload: dict[str, Any] = {"page": {"pageNum": 1, "pageSize": 20}}
    keyword = str(args.get("keyword") or "").strip()
    if keyword:
        payload["name"] = keyword
    data = client.post("/api/mcn/customer-management/list", payload)
    return _json(_slim(data, ("id", "name", "category", "contact", "mobile")))


def _list_contracts(client: McnClient, args: dict[str, Any]) -> str:
    payload: dict[str, Any] = {"page": {"pageNum": 1, "pageSize": 20}}
    keyword = str(args.get("keyword") or "").strip()
    if keyword:
        payload["keyword"] = keyword
    customer_id = str(args.get("customer_id") or "").strip()
    if customer_id:
        payload["customerId"] = customer_id
    data = client.post("/api/mcn/contract/list", payload)
    return _json(
        _slim(
            data,
            ("id", "name", "customerName", "status", "amount", "startTime", "endTime"),
        )
    )


def _get_contract(client: McnClient, args: dict[str, Any]) -> str:
    contract_id = str(args.get("contract_id") or "").strip()
    if not contract_id:
        return "查询合同失败：contract_id 不能为空"
    data = client.get("/api/mcn/contract/queryContractById", {"id": contract_id})
    if not isinstance(data, dict) or not data:
        return f"查询合同失败：合同 {contract_id} 不存在"
    return _json(
        _slim(
            data,
            (
                "id",
                "name",
                "type",
                "customerId",
                "customerName",
                "customerOpportunityId",
                "issuingOffice",
                "signingDate",
                "effectiveTime",
                "expirationTime",
                "enableType",
                "priceVersion",
                "couponVersion",
            ),
        )
    )


def _list_company_entities(client: McnClient, args: dict[str, Any]) -> str:
    data = client.get("/api/amoeba/companyEntity/list")
    return _json(_slim(data, ("id", "companyId", "name", "creditCode", "parentId")))


def _list_opportunities(client: McnClient, args: dict[str, Any]) -> str:
    payload: dict[str, Any] = {
        "page": {
            "pageNum": max(1, int(args.get("page") or 1)),
            "pageSize": min(20, max(1, int(args.get("page_size") or 20))),
        }
    }
    keyword = str(args.get("keyword") or "").strip()
    if keyword:
        payload["keyword"] = keyword
    data = client.post("/api/mcn/customer/opportunity/list", payload)
    return _json(_slim(data, ("id", "name", "customerId", "userName", "createdTime")))


def _list_dictionaries(client: McnClient, args: dict[str, Any]) -> str:
    keys = [str(k).strip() for k in (args.get("enum_keys") or []) if str(k).strip()]
    if not keys:
        keys = ["project_type", "account_platform", "contract_type"]
    data = client.post(
        "/api/mcn/enum/data/dictionary/getEnumList", {"enumKeys": keys}
    )
    if not isinstance(data, list):
        return _json(data)
    out = []
    for group in data:
        if not isinstance(group, dict):
            continue
        values = [
            {"value": item.get("enumValue"), "label": item.get("description")}
            for item in group.get("enumDataDictionaryVos") or []
            if isinstance(item, dict)
        ]
        out.append({"enumKey": group.get("enumKey"), "values": values})
    return _json(out)


def _list_projects(client: McnClient, args: dict[str, Any]) -> str:
    payload: dict[str, Any] = {
        "page": {
            "pageNum": max(1, int(args.get("page") or 1)),
            "pageSize": min(20, max(1, int(args.get("page_size") or 20))),
        }
    }
    customer_id = str(args.get("customer_opportunity_id") or "").strip()
    if customer_id:
        payload["customerId"] = customer_id
    project_type = args.get("project_type")
    if project_type:
        payload["projectTypeList"] = [int(project_type)]
    data = client.post("/api/mcn/customerSubProject/list", payload)
    return _json(
        _slim(
            data,
            (
                "id",
                "projectName",
                "customerName",
                "projectType",
                "projectTypeName",
                "mainPlatform",
                "projectBudget",
                "parentProjectId",
            ),
        )
    )


def _list_price_versions(client: McnClient, args: dict[str, Any]) -> str:
    payload: dict[str, Any] = {"page": {"pageNum": 1, "pageSize": 20}}
    keyword = str(args.get("keyword") or "").strip()
    if keyword:
        payload["name"] = keyword
    data = client.post("/api/mcn/account/quote/version/list", payload)
    return _json(_slim(data, ("id", "name", "version", "isDefault", "status")))


def _list_coupon_versions(client: McnClient, args: dict[str, Any]) -> str:
    payload: dict[str, Any] = {"page": {"pageNum": 1, "pageSize": 20}}
    keyword = str(args.get("keyword") or "").strip()
    if keyword:
        payload["name"] = keyword
    data = client.post("/api/mcn/coupon/version/list", payload)
    return _json(_slim(data, ("id", "name", "version", "isDefault", "status")))


def _parse_quote(client: McnClient, args: dict[str, Any]) -> str:
    raw_text = str(args.get("raw_text") or "").strip()
    product_id = str(args.get("product_id") or "").strip()
    if not raw_text or not product_id:
        return "解析报价失败：raw_text 和 product_id 不能为空"
    return _json(
        client.post(
            "/api/mcn/offerOrder/aiParse",
            {"rawText": raw_text, "productId": product_id},
        )
    )


def _save_quote(client: McnClient, args: dict[str, Any]) -> str:
    if not bool(args.get("confirm")):
        return "未保存：请先把报价单内容给用户核对，用户明确确认后再以 confirm=true 调用。"
    payload = args.get("payload")
    if not isinstance(payload, dict):
        return "未保存：payload 必须是报价单对象"
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    cached = _recall(client, digest)
    if cached is not None:
        return f"重复提交已忽略，上次结果：{cached}"
    result = _json(client.post("/api/mcn/offerOrder/save", payload))
    _remember(client, digest, result)
    return result


def _create_package_quote(client: McnClient, args: dict[str, Any]) -> str:
    payload = _package_quote_payload(client, args)
    if not bool(args.get("confirm")):
        return _json(
            {
                "saved": False,
                "draft": {k: v for k, v in payload.items() if k != "itemRoList"},
                "items": [
                    {
                        "accountName": i["accountName"],
                        "specName": json.loads(i["dynamicItemObj"])["formLabelAlign_name"],
                        "platform": json.loads(i["dynamicItemObj"])["platform"],
                        "quantity": i["quantity"],
                        "price": i["price"],
                        "amount": i["factMoney"],
                    }
                    for i in payload["itemRoList"]
                ],
                "note": "默认字段已按页面规则带出，优惠已试算；只需向用户汇总客户、账号规格、数量和应付金额确认一次。",
            }
        )
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    cached = _recall(client, digest)
    if cached is not None:
        return f"重复提交已忽略，上次结果：{cached}"
    expected_amount = args.get("expected_amount")
    if expected_amount is not None and _money(expected_amount) != _money(payload["factOrderMoney"]):
        raise McnClientError("试算金额与用户确认金额不同，请重新展示草稿确认，未保存")
    saved = client.post("/api/mcn/offerOrder/save", payload)
    # 保存成功就记住结果；回读失败不能让模型再次写入一张相同报价。
    result = _json({"saved": saved, "amount": payload["factOrderMoney"]})
    _remember(client, digest, result)
    try:
        detail = client.get("/api/mcn/offerOrder/detail", {"id": saved})
    except McnClientError:
        return _json({"saved": saved, "verification": "回读失败，请按此 ID 查询，勿重复创建"})
    read_items = detail.get("offerOrderItemVos") or []
    verified = (
        str(detail.get("projectType")) == str(payload["projectType"])
        and _money(detail.get("factOrderMoney")) == _money(payload["factOrderMoney"])
        and len(read_items) == len(payload["itemRoList"])
        and all(
            str(actual.get("skuId")) == expected["skuId"]
            and actual.get("type") == expected["type"]
            and actual.get("dynamicItemObj") == expected["dynamicItemObj"]
            and actual.get("quantity") == expected["quantity"]
            and _money(actual.get("factMoney")) == _money(expected["factMoney"])
            for actual, expected in zip(read_items, payload["itemRoList"], strict=True)
        )
    )
    result = _json(
        {
            "saved": saved,
            "quote": {
                k: detail.get(k)
                for k in (
                    "id",
                    "offerOrderNo",
                    "offerOrderName",
                    "projectType",
                    "orderMoney",
                    "factOrderMoney",
                )
            },
            "itemCount": len(detail.get("offerOrderItemVos") or []),
            "verification": "已核对项目类型、账号规格、数量、回显数据和金额"
            if verified
            else "已保存，但回读字段不一致，需核查；勿重复创建",
        }
    )
    _remember(client, digest, result)
    return result


def _quote_options(client: McnClient, path: str, filters: dict[str, Any]) -> list[dict[str, Any]]:
    """完整查找页面候选项，避免第一页没有默认版本就要求用户手选。"""
    rows: list[dict[str, Any]] = []
    for page in range(1, 21):
        batch = client.post(path, {**filters, "page": {"pageNum": page, "pageSize": 100}})
        if not isinstance(batch, list):
            raise McnClientError("报价候选数据格式不正确，未保存")
        rows.extend(batch)
        if len(batch) < 100:
            return rows
    raise McnClientError("报价候选数据超过查询上限，请缩小范围，未保存")


def _package_quote_payload(client: McnClient, args: dict[str, Any]) -> dict[str, Any]:
    if any(not str(args.get(k) or "").strip() for k in ("name", "product_id", "customer_id")):
        raise McnClientError("报价名称、产品和客户不能为空，未保存")
    specs = args.get("account_specs") or []
    if not isinstance(specs, list) or not specs or len(specs) > 50:
        raise McnClientError("请选择 1 至 50 行账号规格，未保存")
    # 在查询前校验，避免不完整行被静默跳过后保存成另一张报价。
    for spec in specs:
        if not isinstance(spec, dict) or not spec.get("account_id") or not spec.get("sku_id"):
            raise McnClientError("每行必须包含账号和规格 ID，未保存")
        try:
            quantity = _line_quantity(spec)
            raw_quantity = spec.get("quantity", spec.get("count"))
            if raw_quantity not in (None, "") and Decimal(str(raw_quantity)) != quantity:
                raise ValueError("quantity")
            price = _money(spec.get("price"))
            if not price.is_finite() or price < 0:
                raise ValueError("price")
        except (InvalidOperation, ValueError, TypeError, ArithmeticError) as exc:
            raise McnClientError("账号规格的价格或数量不正确，未保存") from exc

    product_id = str(args.get("product_id") or "")
    products = _quote_options(client, "/api/mcn/product/list", {})
    product = next((p for p in products if str(p.get("id")) == product_id), None)
    if not product or product.get("status") == 0:
        raise McnClientError("产品不存在或已停用，未保存")
    attribute = int(product.get("attribute") or -1)
    if attribute in (2, 5, 8):
        raise McnClientError("此工具仅支持账号规格报价，请使用对应产品的报价流程，未保存")

    project_type = args.get("project_type")
    if project_type in (None, "") and int(product.get("productType") or 0) == 1:
        groups = client.post(
            "/api/mcn/enum/data/dictionary/getEnumList", {"enumKeys": ["project_type"]}
        )
        project_type = next(
            (
                item.get("enumValue")
                for group in groups
                if group.get("enumKey") == "project_type"
                for item in group.get("enumDataDictionaryVos") or []
                if item.get("description") == "散投"
            ),
            None,
        )
    if project_type in (None, ""):
        raise McnClientError("缺少项目类型；项目产品请明确业务项目类型，未保存")

    opportunity_id = str(args.get("customer_opportunity_id") or "").strip()
    contract_id = str(args.get("contract_id") or "").strip()
    if opportunity_id and not contract_id:
        contracts = client.post(
            "/api/mcn/contract/list",
            {
                "page": {"pageNum": 1, "pageSize": 1},
                "customerOpportunityId": opportunity_id,
                "validityData": 1,
                "auditStatusList": [1],
            },
        )
        if contracts:
            contract_id = str(contracts[0]["id"])
    protection = (
        client.post(f"/api/mcn/contract/getContractProtectionPrice/bind/{contract_id}", {})
        if contract_id
        else {}
    )
    versions: dict[str, str] = {}
    for key, path, protected_key in (
        ("price_version_id", "/api/mcn/account/quote/version/list", "accountQuoteVersionDos"),
        ("coupon_version_id", "/api/mcn/coupon/version/list", "couponVersionDos"),
    ):
        selected = str(args.get(key) or "").strip()
        if not selected:
            options = protection.get(protected_key) or _quote_options(
                client,
                path,
                {
                    "validityData": 1,
                    **({"searchSource": 2} if key == "coupon_version_id" else {}),
                },
            )
            default = next((v for v in options if v.get("isDefault") == 1), None)
            chosen = options[0] if protection.get(protected_key) else default
            if not chosen and key == "price_version_id" and options:
                chosen = options[0]
            if not chosen:
                raise McnClientError("没有可用默认优惠版本，请用户选择优惠政策名称，未保存")
            selected = str(chosen["id"])
        versions[key] = selected

    candidates = client.post(
        "/api/mcn/account/quotation/queryByProductSpecList",
        {
            "productId": product_id,
            "accountIds": list(dict.fromkeys(str(s["account_id"]) for s in specs)),
            "versionId": versions["price_version_id"],
        },
    )
    items: list[dict[str, Any]] = []
    for spec in specs:
        matches = [
            row
            for row in candidates
            if str(row.get("accountId")) == str(spec["account_id"])
            and str(row.get("id")) == str(spec["sku_id"])
            and (
                not spec.get("supplier_id")
                or str(row.get("supplierId")) == str(spec["supplier_id"])
            )
        ]
        if len(matches) != 1 or matches[0].get("status") == 0:
            raise McnClientError("账号规格不属于所选产品/价格版本、已停用或供应商不明确，未保存")
        row = matches[0]
        quantity = _line_quantity(spec)
        unit = _money(spec["price"] if spec.get("price") not in (None, "") else row.get("price"))
        if (
            not unit.is_finite()
            or unit < 0
            or (spec.get("price") in (None, "") and row.get("price") is None)
        ):
            raise McnClientError("规格没有可用价格，请明确报价，未保存")
        line = (unit * quantity).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        unit_number = _money_number(unit)
        line_number = _money_number(line)
        form = dict(row.get("formLabelAlign") or {})
        form.update(
            {
                k: str(row[k])
                for k in ("ad_category", "publish_category", "publish_type")
                if row.get(k) is not None
            }
        )
        supplier_id = str(row.get("supplierId") or "")
        if supplier_id:
            form["supplier"] = supplier_id
        dynamic = {
            "formLabelAlign": form,
            "formLabelAlign_name": row.get("specName") or "",
            "accountName": row.get("accountName"),
            "platform": row.get("platform"),
            "platformsList": row.get("platformsList"),
            "linkUrl": row.get("linkUrl"),
            "supplierId": supplier_id,
            "spanMethod": [],
        }
        item: dict[str, Any] = {
            "type": 3 if attribute == 7 else 0,
            "modeOperation": row.get("modeOperation") or 1,
            "skuId": str(spec.get("sku_id")),
            "accountId": str(spec.get("account_id")),
            "accountName": row.get("accountName") or "",
            "quantity": quantity,
            "price": unit_number,
            "originalPrice": unit_number,
            "originalMoney": line_number,
            "couponAfterMoney": line_number,
            "factMoney": line_number,
            "noCalculate": 1 if attribute == 7 else 0,
            "advertisingCategoryId": [],
            "dynamicItemObj": json.dumps(dynamic, ensure_ascii=False),
            "costPrice": row.get("costPrice"),
            "costPriceExcludingTax": row.get("costPriceExcludingTax"),
            "taxRate": row.get("taxRate"),
        }
        if supplier_id:
            item["supplierId"] = supplier_id
        items.append(item)
    payload = {
        "offerOrderName": args.get("name"),
        "productId": str(args.get("product_id") or ""),
        "customerId": str(args.get("customer_id") or ""),
        "priceVersion": versions["price_version_id"],
        "couponVersion": versions["coupon_version_id"],
        "projectType": int(str(project_type)),
        "orderType": int(args.get("order_type") or 1),
        "itemRoList": items,
    }
    if opportunity_id:
        payload["customerOpportunityId"] = opportunity_id
    if contract_id:
        payload["contractId"] = contract_id
    calculated = client.post(
        "/api/mcn/offerOrder/calculateCoupon",
        {
            "productId": product_id,
            "contractId": contract_id or None,
            "customerOpportunityId": opportunity_id or None,
            "couponVersion": payload["couponVersion"],
            "projectType": payload["projectType"],
            "orderType": payload["orderType"],
            "orderItems": items,
        },
    )
    calculated_items = calculated.get("orderItems") or []
    if len(calculated_items) != len(items):
        raise McnClientError("优惠试算明细不完整，未保存")
    total = Decimal("0.00")
    for item, result in zip(items, calculated_items, strict=True):
        if str(result.get("skuId")) != item["skuId"] or result.get("couponAfterMoney") is None:
            raise McnClientError("优惠试算规格或金额不一致，未保存")
        amount = _money(result["couponAfterMoney"])
        if not amount.is_finite() or amount < 0:
            raise McnClientError("优惠试算金额不正确，未保存")
        item["couponAfterMoney"] = item["factMoney"] = _money_number(amount)
        item["couponJson"] = json.dumps(result.get("couponDetailVos") or [], ensure_ascii=False)
        dynamic = json.loads(item["dynamicItemObj"])
        dynamic["couponNameList"] = result.get("couponNameList") or []
        item["dynamicItemObj"] = json.dumps(dynamic, ensure_ascii=False)
        total += amount
    payload["orderMoney"] = payload["factOrderMoney"] = _money_number(total)
    return payload


def _create_order_from_quote(client: McnClient, args: dict[str, Any]) -> str:
    if not bool(args.get("confirm")):
        return "未下单：请先把报价单号、客户和金额给用户核对，用户确认后再以 confirm=true 调用。"
    quote_id = str(args.get("quote_id") or "").strip()
    if not quote_id:
        return "下单失败：quote_id 不能为空"
    cache_key = f"order:{quote_id}"
    cached = _recall(client, cache_key)
    if cached is not None:
        return f"重复提交已忽略，上次结果：{cached}"
    detail = client.get("/api/mcn/offerOrder/detail", {"id": quote_id})
    if not isinstance(detail, dict):
        return "下单失败：报价单详情格式不正确"
    items = detail.get("offerOrderItemVos") or []
    if not items:
        return "下单失败：报价单没有明细"
    payload: dict[str, Any] = {
        "submitMethod": 2,
        "offerOrderId": quote_id,
        "orderRo": {
            "customerId": str(detail.get("customerId")),
            "customerOpportunityId": detail.get("customerOpportunityId"),
            "contractId": detail.get("contractId"),
            "productId": str(detail.get("productId")),
            "offerOrderId": quote_id,
            "orderType": detail.get("orderType") or 1,
            "commitType": 1,
            "priceVersionId": str(detail.get("priceVersion")),
            "couponVersionId": str(detail.get("couponVersion")),
            "projectType": detail.get("projectType"),
        },
    }
    project_id = str(args.get("project_id") or "").strip()
    if project_id:
        payload["orderRo"]["projectId"] = project_id
    sub_project_id = str(args.get("sub_project_id") or "").strip()
    if sub_project_id:
        payload["orderRo"]["subProjectId"] = sub_project_id
    payload["offerOrderSaveRo"] = {
        "id": quote_id,
        "offerOrderName": detail.get("offerOrderName"),
        "productId": str(detail.get("productId")),
        "customerId": str(detail.get("customerId")),
        "priceVersion": str(detail.get("priceVersion")),
        "couponVersion": str(detail.get("couponVersion")),
        "orderMoney": detail.get("orderMoney"),
        "factOrderMoney": detail.get("factOrderMoney"),
        "orderType": detail.get("orderType") or 1,
        "projectType": detail.get("projectType"),
        "customerOpportunityId": detail.get("customerOpportunityId"),
        "contractId": detail.get("contractId"),
        **{key: detail.get(key) for key in ("signerId", "signerName", "deptId", "deptName")},
        "itemRoList": [_order_item(item) for item in items],
    }
    payload["orderExtraChargeRoList"] = []
    result = client.post("/api/mcn/order/temporaryStorage", payload)
    text_result = _json(
        {
            "order": result,
            "quoteId": quote_id,
            "quoteNo": detail.get("offerOrderNo"),
            "customerId": str(detail.get("customerId")),
            "amount": detail.get("factOrderMoney"),
        }
    )
    _remember(client, cache_key, text_result)
    return text_result


def _create_combined_order(client: McnClient, args: dict[str, Any]) -> str:
    if not bool(args.get("confirm")):
        return "未下单：请先说明将按供应商拆成几张订单及金额，用户确认后再以 confirm=true 调用。"
    quote_id = str(args.get("quote_id") or "").strip()
    if not quote_id:
        return "下单失败：quote_id 不能为空"
    cache_key = f"combined:{quote_id}"
    cached = _recall(client, cache_key)
    if cached is not None:
        return f"重复提交已忽略，上次结果：{cached}"
    detail = client.get("/api/mcn/offerOrder/detail", {"id": quote_id})
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
            mapped_items.append(_order_item(item))
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
                "projectType": detail.get("projectType"),
                "customerOpportunityId": detail.get("customerOpportunityId"),
                "contractId": detail.get("contractId"),
                **{key: detail.get(key) for key in ("signerId", "signerName", "deptId", "deptName")},
                "itemRoList": mapped_items,
            },
            "orderExtraChargeRoList": [],
        }
        result = client.post("/api/mcn/order/temporaryStorage", payload)
        orders.append({"supplier": supplier, "amount": amount, "result": result})
    text_result = _json(
        {
            "quoteId": quote_id,
            "quoteNo": detail.get("offerOrderNo"),
            "groups": len(orders),
            "orders": orders,
        }
    )
    _remember(client, cache_key, text_result)
    return text_result


# The legacy youli tool entry forwards here so quote/order writes keep one
# implementation. Both surfaces must stay in step; do not fork these again.
save_quote = _save_quote
create_package_quote = _create_package_quote
create_order_from_quote = _create_order_from_quote
create_combined_order = _create_combined_order


def _list_service_items(client: McnClient, args: dict[str, Any]) -> str:
    order_id = str(args.get("order_id") or "").strip()
    if not order_id:
        return "查询服务项目失败：order_id 不能为空"
    data = client.post(
        "/api/mcn/order/service/item/page/list",
        {
            "page": {
                "pageNum": max(1, int(args.get("page") or 1)),
                "pageSize": min(20, max(1, int(args.get("page_size") or 20))),
            },
            "orderId": order_id,
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


def _update_publish_link(client: McnClient, args: dict[str, Any]) -> str:
    link = str(args.get("publish_link") or "").strip()
    if not link:
        return "未保存：发布链接不能为空。"
    if not bool(args.get("confirm")):
        return "未保存：请先让用户核对服务项目、账号规格和链接，确认后再以 confirm=true 调用。"
    payload: dict[str, Any] = {
        "id": str(args.get("service_item_id") or ""),
        "publishLink": link,
    }
    publish_time = str(args.get("publish_time") or "").strip()
    if publish_time:
        # MCN parses this field as a plain date; a time suffix makes it fail.
        payload["publishTime"] = publish_time[:10]
    return _json(client.post("/api/mcn/order/service/item/edit", payload))


def _export_delivery(client: McnClient, args: dict[str, Any]) -> str:
    return export_delivery_result(client, args)


def _list_delivery_projects(client: McnClient, args: dict[str, Any]) -> str:
    return list_delivery_projects(client, args)


def _list_export_templates(client: McnClient, args: dict[str, Any]) -> str:
    return list_export_templates(client, args)


def _get_download_task(client: McnClient, args: dict[str, Any]) -> str:
    return get_download_task(client, args)


def _create_project(client: McnClient, args: dict[str, Any]) -> str:
    name = str(args.get("project_name") or "").strip()
    if not name:
        return "未创建：项目名称不能为空。"
    if not bool(args.get("confirm")):
        return "未创建：请先和用户确认项目名称，确认后再以 confirm=true 调用。"
    return _json(
        client.post("/api/mcn/customerProject/addProject", {"projectName": name})
    )


def _create_sub_project(client: McnClient, args: dict[str, Any]) -> str:
    name = str(args.get("project_name") or "").strip()
    if not name:
        return "未创建：project_name 不能为空。"
    if not bool(args.get("confirm")):
        return "未创建：请先和用户确认项目名称、客户、类型和平台，确认后再以 confirm=true 调用。"
    opportunity_id = str(args.get("customer_opportunity_id") or "").strip()
    customer_name = str(args.get("customer_name") or "").strip()
    if not opportunity_id:
        return "未创建：customer_opportunity_id 不能为空。"
    if not customer_name:
        return "未创建：customer_name 不能为空。"
    payload: dict[str, Any] = {
        "projectName": name,
        "customerId": opportunity_id,
        "customerName": customer_name,
        "projectType": int(args.get("project_type") or 1),
        "mainPlatform": int(args.get("main_platform") or 1),
    }
    if args.get("project_budget") not in (None, ""):
        payload["projectBudget"] = float(args.get("project_budget") or 0)
    leader_ids = [str(x) for x in (args.get("project_leader_ids") or []) if str(x).strip()]
    if leader_ids:
        payload["projectLeaderIds"] = leader_ids
    parent_id = str(args.get("parent_project_id") or "").strip()
    if parent_id:
        payload["parentProjectId"] = parent_id
    return _json(client.post("/api/mcn/customerSubProject/add", payload))


def _create_coupon_version(client: McnClient, args: dict[str, Any]) -> str:
    name = str(args.get("name") or "").strip()
    if not name:
        return "未创建：版本名称不能为空。"
    if not bool(args.get("confirm")):
        return "未创建：请先和用户确认版本名称、客户和有效期，确认后再以 confirm=true 调用。"
    payload: dict[str, Any] = {
        "name": name,
        "isDefault": 1 if bool(args.get("is_default")) else 0,
    }
    version = str(args.get("version") or "").strip()
    if version:
        payload["version"] = version
    opportunity_id = str(args.get("opportunity_id") or "").strip()
    if opportunity_id:
        payload["opportunityId"] = opportunity_id
    effective_time = str(args.get("effective_time") or "").strip()
    if effective_time:
        payload["effectiveTime"] = effective_time
    expiration_time = str(args.get("expiration_time") or "").strip()
    if expiration_time:
        payload["expirationTime"] = expiration_time
    return _json(
        client.post("/api/mcn/coupon/version/addCouponVersion", payload)
    )


def _create_contract(client: McnClient, args: dict[str, Any]) -> str:
    if not bool(args.get("confirm")):
        return "未创建：请先和用户核对合同类型、客户、保价政策和有效期，确认后再以 confirm=true 调用。"
    payload = args.get("payload")
    if payload is None:
        payload = {
            "enableType": int(args.get("enable_type") or 2),
            "type": int(args.get("type") or 1),
            "name": str(args.get("name") or "").strip(),
            "customerId": str(args.get("customer_id") or ""),
            "customerOpportunityId": str(args.get("customer_opportunity_id") or ""),
            "issuingOffice": str(args.get("issuing_office") or ""),
            "signingDate": str(args.get("signing_date") or args.get("effective_time") or ""),
            "effectiveTime": str(args.get("effective_time") or ""),
            "expirationTime": str(args.get("expiration_time") or ""),
            "orderNumber": 0,
            "orderAmount": 0,
            "amountReceived": 0,
            "pendingAmount": 0,
            "contractAccountInfo": args.get("contract_account_info") or {"periodType": 1},
        }
        if not payload["name"] or not payload["issuingOffice"]:
            return "未创建：name、issuing_office 不能为空。"
        if not payload["customerOpportunityId"]:
            return (
                "未创建：customer_opportunity_id 不能为空，否则合同不会出现在合同列表；"
                "请先调用 youli_list_opportunities 选择商机。"
            )
        if not payload["customerId"]:
            payload.pop("customerId", None)
        if not payload["effectiveTime"] or not payload["expirationTime"]:
            return "未创建：effective_time、expiration_time 不能为空。"
        price_version = str(args.get("price_version_id") or "").strip()
        coupon_version = str(args.get("coupon_version_id") or "").strip()
        if price_version or coupon_version:
            payload["protectionPrice"] = 1
            if price_version:
                payload["priceVersion"] = price_version
                payload["priceVersionBindList"] = [{"versionId": price_version}]
            if coupon_version:
                payload["couponVersion"] = coupon_version
                payload["couponVersionBindList"] = [{"versionId": coupon_version}]
        else:
            payload["protectionPrice"] = 0
    if not isinstance(payload, dict):
        return "未创建：payload 必须是合同对象"
    if not payload.get("enableType"):
        return "未创建：enableType 不能为空。"
    return _json(client.post("/api/mcn/contract/addContract", payload))


_HANDLERS = {
    "youli_list_accounts": ("查询账号", _list_accounts),
    "youli_query_account_specs": ("查询账号规格", _query_account_specs),
    "youli_list_quotes": ("查询报价单", _list_quotes),
    "youli_get_quote": ("查询报价单详情", _get_quote),
    "youli_list_products": ("查询产品", _list_products),
    "youli_list_customers": ("查询客户", _list_customers),
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
    "youli_list_delivery_projects": ("查询交付项目", _list_delivery_projects),
    "youli_list_export_templates": ("查询导出模板", _list_export_templates),
    "youli_get_download_task": ("查询导出文件", _get_download_task),
    "youli_export_delivery": ("导出交付数据", _export_delivery),
    "youli_create_project": ("创建项目", _create_project),
    "youli_create_sub_project": ("创建项目", _create_sub_project),
    "youli_create_coupon_version": ("创建优惠政策版本", _create_coupon_version),
    "youli_create_contract": ("创建合同", _create_contract),
}


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


TOOLS: list[dict[str, Any]] = [
    {
        "name": "youli_list_accounts",
        "description": "查询经贝 MCN 账号库，拉包报价先选账号时使用。",
        "inputSchema": _schema(
            {
                "account_name": {"type": "string", "description": "账号名称关键字，可空"},
                "platform": {"type": "integer", "description": "媒体平台：1抖音 2快手 3小红书 4微博 5微信 6今日头条 7B站；0 表示不限"},
                "page": {"type": "integer", "description": "页码，从 1 开始"},
                "page_size": {"type": "integer", "description": "每页数量，最大 20"},
            }
        ),
    },
    {
        "name": "youli_query_account_specs",
        "description": "批量查询所选账号的可用规格与价格，拉包报价第二步使用。",
        "inputSchema": _schema(
            {
                "product_id": {"type": "string", "description": "产品 ID"},
                "account_ids": {"type": "array", "items": {"type": "string"}, "description": "账号 ID 列表，最多 50 个"},
                "version_id": {"type": "string", "description": "价格版本 ID，可空"},
            },
            ["product_id", "account_ids"],
        ),
    },
    {
        "name": "youli_list_quotes",
        "description": "查询经贝 MCN 报价单列表。",
        "inputSchema": _schema(
            {
                "keyword": {"type": "string", "description": "报价单名称或编号关键字，可空"},
                "page": {"type": "integer"},
                "page_size": {"type": "integer"},
            }
        ),
    },
    {
        "name": "youli_get_quote",
        "description": "读取经贝 MCN 报价单详情，含明细、账号、规格、数量和金额。",
        "inputSchema": _schema({"quote_id": {"type": "string"}}, ["quote_id"]),
    },
    {
        "name": "youli_list_products",
        "description": "查询经贝 MCN 产品列表，用于确定报价单的 productId。",
        "inputSchema": _schema({"keyword": {"type": "string"}}),
    },
    {
        "name": "youli_list_customers",
        "description": "查询经贝 MCN 客户列表，用于确定客户和商机。",
        "inputSchema": _schema({"keyword": {"type": "string"}}),
    },
    {
        "name": "youli_list_contracts",
        "description": "查询经贝 MCN 销售合同。",
        "inputSchema": _schema(
            {
                "keyword": {"type": "string"},
                "customer_id": {"type": "string"},
            }
        ),
    },
    {
        "name": "youli_get_contract",
        "description": "读取单份销售合同详情：类型、客户、商机、签约主体、有效期和保价版本。",
        "inputSchema": _schema(
            {"contract_id": {"type": "string"}},
            ["contract_id"],
        ),
    },
    {
        "name": "youli_list_company_entities",
        "description": "查询当前机构可用的签约主体（开票单位），创建合同时必填 issuing_office。",
        "inputSchema": _schema({}),
    },
    {
        "name": "youli_list_opportunities",
        "description": "查询客户商机列表，创建合同/项目/报价时用于确定 customer_opportunity_id。",
        "inputSchema": _schema(
            {
                "keyword": {"type": "string", "description": "客户或商机名称关键字，可空"},
                "page": {"type": "integer"},
                "page_size": {"type": "integer"},
            }
        ),
    },
    {
        "name": "youli_list_dictionaries",
        "description": "查询业务字典（项目类型 project_type、平台 account_platform、合同类型 contract_type 等）。",
        "inputSchema": _schema(
            {
                "enum_keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "字典 key 列表，可空；默认 project_type/account_platform/contract_type",
                }
            }
        ),
    },
    {
        "name": "youli_list_projects",
        "description": "查询客户项目列表（含项目类型、主平台、预算）。",
        "inputSchema": _schema(
            {
                "customer_opportunity_id": {"type": "string", "description": "客户商机 ID，可空"},
                "project_type": {"type": "integer", "description": "项目类型，可空"},
                "page": {"type": "integer"},
                "page_size": {"type": "integer"},
            }
        ),
    },
    {
        "name": "youli_list_price_versions",
        "description": "查询经贝 MCN 账号价格版本。",
        "inputSchema": _schema({"keyword": {"type": "string"}}),
    },
    {
        "name": "youli_list_coupon_versions",
        "description": "查询经贝 MCN 优惠政策版本。",
        "inputSchema": _schema({"keyword": {"type": "string"}}),
    },
    {
        "name": "youli_parse_quote",
        "description": "把客户需求原文解析成结构化报价单草稿。",
        "inputSchema": _schema(
            {
                "raw_text": {"type": "string"},
                "product_id": {"type": "string"},
            },
            ["raw_text", "product_id"],
        ),
    },
    {
        "name": "youli_save_quote",
        "description": "保存经贝 MCN 报价单（写操作，需用户确认）。",
        "inputSchema": _schema(
            {
                "payload": {"type": "object", "description": "OfferOrderSaveRo 结构的报价单"},
                "confirm": {"type": "boolean", "description": "用户核对并明确确认后才传 true"},
            },
            ["payload", "confirm"],
        ),
    },
    {
        "name": "youli_create_package_quote",
        "description": "创建账号报价：先用 confirm=false 生成只读草稿，自动带出散投项目类型、常规单、商机首个有效合同、合同保价或默认价格/优惠版本、账号规格与价格，并调用页面同一优惠试算。不要向用户索要 ID，不要逐个确认默认字段；只追问账号平台/规格等业务歧义。汇总草稿金额一次确认后 confirm=true 保存；不需要再次确认用户已选过的规格。接口为空只说明当前查询无结果，不可断言接口坏了或系统没有其他记录。",
        "inputSchema": _schema(
            {
                "name": {"type": "string", "description": "报价单名称"},
                "product_id": {"type": "string"},
                "customer_id": {"type": "string"},
                "price_version_id": {"type": "string", "description": "用户指定时传；省略则自动按页面规则选择"},
                "coupon_version_id": {"type": "string", "description": "用户指定时传；省略则自动按页面规则选择"},
                "project_type": {"type": "integer", "description": "散投自动带出；项目产品需明确业务类型"},
                "order_type": {"type": "integer", "description": "默认常规单 1"},
                "contract_id": {"type": "string", "description": "可选，默认商机下首个有效已审核合同"},
                "expected_amount": {"type": "number", "description": "confirm=true 时传入用户已确认的草稿应付金额；金额变化会阻止保存"},
                "customer_opportunity_id": {
                    "type": "string",
                    "description": "客户商机 ID，可空；传了报价和后续订单才能归到项目",
                },
                "account_specs": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "每项包含 account_id、sku_id；price 可省略以取规格价格，quantity 默认 1。账号名、规格名、平台、运营方式自动查询；渠道存在多个供应商时指定 supplier_id。",
                },
                "confirm": {"type": "boolean"},
            },
            [
                "name",
                "product_id",
                "customer_id",
                "account_specs",
                "confirm",
            ],
        ),
    },
    {
        "name": "youli_create_order_from_quote",
        "description": "从报价单创建并提交销售订单（写操作，需用户确认）。",
        "inputSchema": _schema(
            {
                "quote_id": {"type": "string"},
                "project_id": {
                    "type": "string",
                    "description": "娱乐/品牌名对应的父项目 ID，可空；填了订单才能按项目统计交付",
                },
                "sub_project_id": {"type": "string", "description": "子项目 ID，可空"},
                "confirm": {"type": "boolean"},
            },
            ["quote_id", "confirm"],
        ),
    },
    {
        "name": "youli_create_combined_order",
        "description": "组合订单：按供应商把一张拉包报价拆成多张订单（写操作，需用户确认）。",
        "inputSchema": _schema(
            {"quote_id": {"type": "string"}, "confirm": {"type": "boolean"}},
            ["quote_id", "confirm"],
        ),
    },
    {
        "name": "youli_list_service_items",
        "description": "查询订单服务项目（账号+规格+发布链接）。",
        "inputSchema": _schema(
            {
                "order_id": {"type": "string"},
                "page": {"type": "integer"},
                "page_size": {"type": "integer"},
            },
            ["order_id"],
        ),
    },
    {
        "name": "youli_update_publish_link",
        "description": "填写服务项目发布链接（写操作，需用户确认）。",
        "inputSchema": _schema(
            {
                "service_item_id": {"type": "string"},
                "publish_link": {"type": "string"},
                "publish_time": {"type": "string", "description": "发布时间 yyyy-MM-dd；时分秒会被忽略"},
                "confirm": {"type": "boolean"},
            },
            ["service_item_id", "publish_link", "confirm"],
        ),
    },
    {
        "name": "youli_export_delivery",
        "description": EXPORT_DESCRIPTION,
        "inputSchema": _schema(
            {
                "mode": {
                    "type": "string",
                    "description": "project 按项目，link 按每条发布链接。不要把英文值问给用户",
                },
                "project_name": {"type": "string"},
                "customer_opportunity_id": {"type": "string"},
                "export_template_id": {
                    "type": "string",
                    "description": "仅按链接导出且用户指定了模板名称时传入。不要问用户要 ID",
                },
                "confirm": {"type": "boolean"},
            },
            ["mode", "confirm"],
        ),
    },
    {
        "name": "youli_list_delivery_projects",
        "description": LIST_DELIVERY_PROJECTS_DESCRIPTION,
        "inputSchema": _schema(
            {
                "keyword": {"type": "string", "description": "用户说的或截图上的项目名，可空"},
                "name_field": {
                    "type": "string",
                    "description": "project 查娱乐/品牌名，brand 查订单项目名",
                },
            },
            [],
        ),
    },
    {
        "name": "youli_list_export_templates",
        "description": LIST_EXPORT_TEMPLATES_DESCRIPTION,
        "inputSchema": _schema(
            {
                "template_name": {
                    "type": "string",
                    "description": "模板名称，可空表示列出启用的按链接模板",
                }
            },
            [],
        ),
    },
    {
        "name": "youli_get_download_task",
        "description": GET_DOWNLOAD_TASK_DESCRIPTION,
        "inputSchema": _schema(
            {
                "task_id": {"type": "string", "description": "导出返回的任务 ID"},
                "file_prefix": {
                    "type": "string",
                    "description": "没有任务 ID 时使用导出返回的 file_prefix",
                },
            },
            [],
        ),
    },
    {
        "name": "youli_create_project",
        "description": "创建客户父项目（写操作，需用户确认）。子项目请用 youli_create_sub_project。",
        "inputSchema": _schema(
            {"project_name": {"type": "string"}, "confirm": {"type": "boolean"}},
            ["project_name", "confirm"],
        ),
    },
    {
        "name": "youli_create_sub_project",
        "description": "创建客户子项目，支持不同项目类型（project_type）和主平台（main_platform），写操作需用户确认。",
        "inputSchema": _schema(
            {
                "project_name": {"type": "string"},
                "customer_opportunity_id": {"type": "string"},
                "customer_name": {"type": "string"},
                "project_type": {"type": "integer", "description": "项目类型，先用 youli_list_dictionaries 查询 project_type"},
                "main_platform": {"type": "integer", "description": "主平台，1抖音 2快手 3小红书 4微博 5微信 6今日头条 7B站"},
                "project_budget": {"type": "number", "description": "项目预算，可空"},
                "project_leader_ids": {"type": "array", "items": {"type": "string"}, "description": "项目负责人员工 ID 列表，可空"},
                "parent_project_id": {"type": "string", "description": "父项目 ID，可空"},
                "confirm": {"type": "boolean"},
            },
            ["project_name", "customer_opportunity_id", "customer_name", "project_type", "main_platform", "confirm"],
        ),
    },
    {
        "name": "youli_create_coupon_version",
        "description": "创建优惠政策版本，含客户特定版本（写操作，需用户确认）。",
        "inputSchema": _schema(
            {
                "name": {"type": "string"},
                "version": {"type": "string"},
                "opportunity_id": {"type": "string"},
                "is_default": {"type": "boolean"},
                "effective_time": {"type": "string"},
                "expiration_time": {"type": "string"},
                "confirm": {"type": "boolean"},
            },
            ["name", "confirm"],
        ),
    },
    {
        "name": "youli_create_contract",
        "description": "创建销售合同（写操作，需用户确认）。合同类型 type：1单次 2季框 3半年框 4年框；签约主体 issuing_office 先通过 youli_list_company_entities 获取。",
        "inputSchema": _schema(
            {
                "payload": {"type": "object", "description": "ContractRo 结构"},
                "name": {"type": "string", "description": "合同名称"},
                "customer_id": {"type": "string", "description": "签约客户 ID"},
                "customer_opportunity_id": {"type": "string", "description": "客户商机 ID；填了合同才会出现在合同列表"},
                "type": {"type": "integer", "description": "合同类型 1单次 2季框 3半年框 4年框"},
                "issuing_office": {"type": "string", "description": "签约主体/开票单位 ID"},
                "effective_time": {"type": "string", "description": "有效开始日期 yyyy-MM-dd"},
                "expiration_time": {"type": "string", "description": "有效截止日期 yyyy-MM-dd"},
                "signing_date": {"type": "string", "description": "签约日期 yyyy-MM-dd，可空"},
                "price_version_id": {"type": "string", "description": "保价价格版本 ID，可空"},
                "coupon_version_id": {"type": "string", "description": "保价优惠版本 ID，可空"},
                "enable_type": {"type": "integer", "description": "1临时启用 2正式启用"},
                "contract_account_info": {
                    "type": "object",
                    "description": "账期信息，默认 {periodType:1}",
                },
                "confirm": {"type": "boolean"},
            },
            ["confirm"],
        ),
    },
]


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
