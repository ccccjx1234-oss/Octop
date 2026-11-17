"""MCN opportunity resolution, quote parsing, and package quote workflows."""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from octop.infra.auth.youli.mcn_client import McnClient, McnClientError
from octop.infra.auth.youli.pagination import post_all_pages

from .common import (
    _json,
    _line_quantity,
    _money,
    _money_number,
    _recall,
    _remember,
    idempotent_post,
    payload_digest,
)


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


def _opportunity_rows(client: McnClient, *, customer_id: str = "") -> list[dict[str, Any]]:
    payload: dict[str, Any] = {"useDataPermission": True}
    if customer_id:
        payload["customerId"] = customer_id
    try:
        rows = post_all_pages(
            client,
            "/api/mcn/customer/opportunity/list",
            payload,
            page_size=100,
            max_pages=10,
        )
    except McnClientError:
        return []
    return [
        r
        for r in rows
        if isinstance(r, dict)
        and r.get("customerId") is not None
        and (r.get("employeeId") is not None or r.get("deptOneId") is not None)
    ]


def _find_opportunity(
    client: McnClient,
    *,
    customer_id: str = "",
    opportunity_id: str = "",
    multi_error: str,
) -> dict[str, Any] | None:
    """Resolve MCN opportunity row from either opportunity_id or unique customer_id match."""
    rows = _opportunity_rows(client)
    if not rows:
        return None
    target_opp = opportunity_id.strip()
    target_cust = customer_id.strip()
    if target_opp:
        matched = next((r for r in rows if str(r.get("id")) == target_opp), None)
        if matched:
            return matched
        if target_cust and len(rows) >= 100:
            cust_rows = _opportunity_rows(client, customer_id=target_cust)
            matched = next((r for r in cust_rows if str(r.get("id")) == target_opp), None)
            if matched:
                return matched
        if not target_cust or target_cust == target_opp:
            cm = [r for r in rows if str(r.get("customerId")) == target_opp]
            if not cm and len(rows) >= 100:
                cm = _opportunity_rows(client, customer_id=target_opp)
            if len(cm) == 1:
                return cm[0]
            if len(cm) > 1:
                raise McnClientError(multi_error)
        return None
    if target_cust:
        by_id = [r for r in rows if str(r.get("id")) == target_cust]
        if len(by_id) == 1:
            return by_id[0]
        cm = [r for r in rows if str(r.get("customerId")) == target_cust]
        if not cm and len(rows) >= 100:
            cm = _opportunity_rows(client, customer_id=target_cust)
        if len(cm) == 1:
            return cm[0]
        if len(cm) > 1:
            raise McnClientError(multi_error)
    return None


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
    if not isinstance(payload, dict) or not payload:
        return "未保存：payload 必须是报价单对象"
    digest = payload_digest(payload)
    normalized = (
        {
            "isCalculate": 1,
            "otherOffers": 0,
            "offerExtraChargeRoList": [],
            **payload,
        }
        if "itemRoList" in payload
        else payload
    )
    return idempotent_post(client, digest, "/api/mcn/offerOrder/save", normalized)


def _validate_package_specs(specs: Any) -> list[dict[str, Any]]:
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
    return specs


def _resolve_package_product_and_project_type(
    client: McnClient,
    product_id: str,
    raw_project_type: Any,
) -> tuple[int, Any]:
    products = _quote_options(client, "/api/mcn/product/list", {})
    product = next((p for p in products if str(p.get("id")) == product_id), None)
    if not product or product.get("status") == 0:
        raise McnClientError("产品不存在或已停用，未保存")
    attribute = int(product.get("attribute") or -1)
    if attribute in (2, 5, 8):
        raise McnClientError("此工具仅支持账号规格报价，请使用对应产品的报价流程，未保存")
    project_type = raw_project_type
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
    return attribute, project_type


def _resolve_package_versions(
    client: McnClient,
    args: dict[str, Any],
    opportunity_id: str,
) -> tuple[str, dict[str, str]]:
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
    return contract_id, versions


def _build_package_items(
    client: McnClient,
    specs: list[dict[str, Any]],
    *,
    product_id: str,
    price_version_id: str,
    attribute: int,
) -> list[dict[str, Any]]:
    candidates = client.post(
        "/api/mcn/account/quotation/queryByProductSpecList",
        {
            "productId": product_id,
            "accountIds": list(dict.fromkeys(str(s["account_id"]) for s in specs)),
            "versionId": price_version_id,
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
        spec_name = row.get("specName") or ""
        note_desc = str(spec.get("note_description") or "").strip()
        dynamic: dict[str, Any] = {
            "formLabelAlign": form,
            "formLabelAlign_name": spec_name,
            "accountName": row.get("accountName"),
            "accountId": str(spec.get("account_id")),
            "platform": row.get("platform"),
            "platformsList": row.get("platformsList"),
            "linkUrl": row.get("linkUrl"),
            "supplierId": supplier_id,
            "officialPlatformCooperation": row.get("officialPlatformCooperation"),
            "platformAccountId": row.get("platformAccountId"),
            "noteDescription": note_desc,
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
            "platformPrice": 0,
            "rebate": "",
            "gift": 0,
            "distribute": 0,
            "noCalculate": 1 if attribute == 7 else 0,
            "advertisingCategoryId": [],
            "formLabelAlign": json.dumps(form, ensure_ascii=False),
            "formLabelAlignName": spec_name,
            "linkUrl": row.get("linkUrl") or "",
            "noteDescription": note_desc,
            "dynamicItemObj": json.dumps(dynamic, ensure_ascii=False),
            "costPrice": row.get("costPrice"),
            "costPriceExcludingTax": row.get("costPriceExcludingTax"),
            "taxRate": row.get("taxRate"),
        }
        if supplier_id:
            item["supplierId"] = supplier_id
        if row.get("supplierCooperationStatus") not in (None, ""):
            item["supplierCooperationStatus"] = row.get("supplierCooperationStatus")
        items.append(item)
    return items


def _apply_coupon_calculation(
    client: McnClient,
    payload: dict[str, Any],
    items: list[dict[str, Any]],
) -> None:
    calculated = client.post(
        "/api/mcn/offerOrder/calculateCoupon",
        {
            "productId": payload["productId"],
            "contractId": payload.get("contractId") or None,
            "customerOpportunityId": payload.get("customerOpportunityId") or None,
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
        if result.get("couponMoney") is not None:
            item["couponMoney"] = _money_number(_money(result["couponMoney"]))
        item["couponJson"] = json.dumps(result.get("couponDetailVos") or [], ensure_ascii=False)
        dynamic = json.loads(item["dynamicItemObj"])
        dynamic["couponNameList"] = result.get("couponNameList") or []
        item["dynamicItemObj"] = json.dumps(dynamic, ensure_ascii=False)
        total += amount
    payload["orderMoney"] = payload["factOrderMoney"] = _money_number(total)


def _package_quote_payload(client: McnClient, args: dict[str, Any]) -> dict[str, Any]:
    if any(not str(args.get(k) or "").strip() for k in ("name", "product_id", "customer_id")):
        raise McnClientError("报价名称、产品和客户不能为空，未保存")
    specs = _validate_package_specs(args.get("account_specs"))
    product_id = str(args.get("product_id") or "")
    attribute, project_type = _resolve_package_product_and_project_type(
        client, product_id, args.get("project_type")
    )

    raw_cust_id = str(args.get("customer_id") or "").strip()
    opportunity_id = str(args.get("customer_opportunity_id") or "").strip()
    matched_opp = _find_opportunity(
        client,
        customer_id=raw_cust_id,
        opportunity_id=opportunity_id,
        multi_error=(
            "该客户存在多个商机，请先调用 youli_list_opportunities 选择 customer_opportunity_id "
            "后再创建报价，否则报价单列表无法显示客户名称，未保存"
        ),
    )
    resolved_cust_id = raw_cust_id
    if matched_opp:
        opportunity_id = str(matched_opp["id"])
        if matched_opp.get("customerId") not in (None, ""):
            resolved_cust_id = str(matched_opp["customerId"]).strip()

    contract_id, versions = _resolve_package_versions(client, args, opportunity_id)
    items = _build_package_items(
        client,
        specs,
        product_id=product_id,
        price_version_id=versions["price_version_id"],
        attribute=attribute,
    )
    payload: dict[str, Any] = {
        "offerOrderName": args.get("name"),
        "productId": product_id,
        "customerId": resolved_cust_id,
        "priceVersion": versions["price_version_id"],
        "couponVersion": versions["coupon_version_id"],
        "projectType": int(str(project_type)),
        "orderType": int(args.get("order_type") or 1),
        "otherOffers": 0,
        "isCalculate": 1,
        "exportTemplateId": None,
        "quoteDataFields": "",
        "offerExtraChargeRoList": [],
        "itemRoList": items,
    }
    opp_fallbacks = {
        "signerId": matched_opp.get("employeeId") if matched_opp else None,
        "signerName": matched_opp.get("userName") if matched_opp else None,
        "deptId": matched_opp.get("deptOneId") if matched_opp else None,
        "deptName": matched_opp.get("deptOneName") if matched_opp else None,
    }
    for arg_key, api_key in (
        ("signer_id", "signerId"),
        ("signer_name", "signerName"),
        ("dept_id", "deptId"),
        ("dept_name", "deptName"),
    ):
        val = args.get(arg_key)
        if val in (None, ""):
            val = opp_fallbacks.get(api_key)
        if val not in (None, ""):
            payload[api_key] = val
    if opportunity_id:
        payload["customerOpportunityId"] = opportunity_id
    if contract_id:
        payload["contractId"] = contract_id
    _apply_coupon_calculation(client, payload, items)
    return payload


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
    digest = payload_digest(payload)
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
