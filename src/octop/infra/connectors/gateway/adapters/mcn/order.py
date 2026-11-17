"""MCN order creation (single and combined by supplier) and publish link updates."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from octop.infra.auth.youli.mcn_client import McnClient, McnClientError
from octop.infra.auth.youli.pagination import post_all_pages

from .common import (
    _json,
    _money,
    _money_number,
    _normalize_date,
    _parse_json_obj,
    _recall,
    _remember,
)


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


def _order_item(item: dict[str, Any]) -> dict[str, Any]:
    dynamic_raw = item.get("dynamicItemObj")
    dynamic_obj = _parse_json_obj(dynamic_raw)
    form_obj = item.get("formLabelAlign")
    if not form_obj and isinstance(dynamic_obj.get("formLabelAlign"), dict):
        form_obj = json.dumps(dynamic_obj["formLabelAlign"], ensure_ascii=False)
    elif isinstance(form_obj, dict):
        form_obj = json.dumps(form_obj, ensure_ascii=False)
    form_name = (
        item.get("formLabelAlignName")
        or item.get("formLabelAlign_name")
        or item.get("specName")
        or dynamic_obj.get("formLabelAlign_name")
        or ""
    )
    coupon_json = item.get("couponJson")
    if coupon_json in (None, "") and isinstance(item.get("couponDetailVos"), list):
        coupon_json = json.dumps(item["couponDetailVos"], ensure_ascii=False)
    dynamic_text = (
        dynamic_raw
        if isinstance(dynamic_raw, str)
        else json.dumps(dynamic_obj, ensure_ascii=False)
        if dynamic_obj
        else None
    )
    payload: dict[str, Any] = {
        "itemId": None,
        "type": item.get("type"),
        "skuId": item.get("skuId"),
        "accountId": item.get("accountId"),
        "accountName": item.get("accountName"),
        "packageId": item.get("packageId"),
        "count": item.get("count"),
        "quantity": item.get("quantity"),
        "price": item.get("originalPrice")
        if item.get("originalPrice") is not None
        else item.get("price"),
        "originalPrice": item.get("originalPrice"),
        "originalMoney": item.get("originalMoney"),
        "couponMoney": item.get("couponMoney"),
        "couponJson": coupon_json,
        "couponAfterMoney": (
            item.get("baseMoney")
            if item.get("baseMoney") is not None
            else item.get("couponAfterMoney")
        ),
        "factMoney": item.get("factMoney"),
        "factRate": item.get("factRate"),
        "modeOperation": item.get("modeOperation"),
        "noCalculate": item.get("noCalculate"),
        "supplierId": item.get("supplierId") or _item_supplier(item),
        "supplierCooperationStatus": item.get("supplierCooperationStatus"),
        "advertisingCategoryId": item.get("advertisingCategoryId"),
        "costPrice": item.get("costPrice"),
        "costPriceExcludingTax": item.get("costPriceExcludingTax"),
        "taxRate": item.get("taxRate"),
        "platformPrice": item.get("platformPrice"),
        "rebate": item.get("rebate"),
        "gift": item.get("gift"),
        "giftTypeId": item.get("giftTypeId"),
        "giftTypeName": item.get("giftTypeName"),
        "distribute": item.get("distribute"),
        "distributeSourceAccountId": item.get("distributeSourceAccountId"),
        "distributeSourceAccountName": item.get("distributeSourceAccountName"),
        "distributeRemark": item.get("distributeRemark"),
        "formLabelAlign": form_obj,
        "formLabelAlignName": form_name or None,
        "dynamicItemObj": dynamic_text,
        "isAuditExempt": item.get("isAuditExempt"),
        "noteDescription": item.get("noteDescription"),
    }
    return {k: v for k, v in payload.items() if k == "itemId" or v is not None}


def _resolve_sub_project_for_order(
    client: McnClient | None,
    sub_project_id: str,
    project_id: str,
    opportunity_id: Any,
) -> str:
    """Validate sub-project opportunity ownership and inherit parentProjectId if omitted."""
    if client is None or not sub_project_id:
        return project_id
    try:
        rows = post_all_pages(
            client,
            "/api/mcn/customerSubProject/list",
            {},
            page_size=100,
            max_pages=10,
        )
    except McnClientError:
        return project_id
    real_rows = [
        r
        for r in rows
        if isinstance(r, dict)
        and (r.get("subProjectCode") is not None or r.get("parentProjectId") is not None)
    ]
    if not real_rows:
        return project_id
    sub = next((r for r in real_rows if str(r.get("id")) == sub_project_id), None)
    if not sub:
        return project_id
    sub_cust = str(sub.get("customerId") or "").strip()
    opp_text = str(opportunity_id or "").strip()
    if sub_cust and opp_text and sub_cust != opp_text:
        raise McnClientError("下单失败：所选子项目与报价单客户商机不一致，请重新选择")
    parent_id = str(sub.get("parentProjectId") or "").strip()
    if parent_id:
        if project_id and project_id != parent_id:
            raise McnClientError("下单失败：传入的 project_id 与子项目所属父项目不一致")
        if not project_id:
            return parent_id
    return project_id


def _validate_order_rebate(commit_type: int, items: list[dict[str, Any]]) -> None:
    """Mirror OrderServiceImpl.validateLinePaymentDiscountVsFloor for commit_type in (1, 3, 4)."""
    if commit_type not in (1, 3, 4):
        return
    for item in items:
        floor_raw = (
            item.get("baseMoney")
            if item.get("baseMoney") is not None
            else item.get("couponAfterMoney")
        )
        if item.get("factMoney") is None or floor_raw is None:
            continue
        fact_val = _money(item.get("factMoney"))
        floor_val = _money(floor_raw)
        rebate = _money(item.get("rebate"))
        if fact_val <= floor_val:
            if rebate != Decimal("0.00"):
                raise McnClientError("下单失败：实付金额小于等于底价时不允许填写返点/回扣金额")
        elif rebate > (fact_val - floor_val):
            raise McnClientError("下单失败：返点/回扣金额不能超过实付金额与底价的差额")


def _build_order_payload(
    detail: dict[str, Any],
    args: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    client: McnClient | None = None,
    quote_id: str | None = None,
    order_money: Any | None = None,
    fact_order_money: Any | None = None,
) -> dict[str, Any]:
    opportunity_id = args.get("customer_opportunity_id") or detail.get("customerOpportunityId")
    if not str(opportunity_id or "").strip():
        raise McnClientError(
            "报价单缺少客户商机 ID（customerOpportunityId），MCN 订单详情页需要商机关联；请先在报价单补充商机后再下单"
        )
    commit_type = int(args.get("commit_type") or detail.get("commitType") or 1)
    _validate_order_rebate(commit_type, items)
    amount_val = fact_order_money if fact_order_money is not None else detail.get("factOrderMoney")
    money_val = order_money if order_money is not None else detail.get("orderMoney")
    order_ro: dict[str, Any] = {
        "customerId": str(detail.get("customerId") or ""),
        "customerOpportunityId": opportunity_id,
        "contractId": detail.get("contractId"),
        "productId": str(detail.get("productId") or ""),
        "orderType": detail.get("orderType") or 1,
        "commitType": commit_type,
        "priceVersionId": str(detail.get("priceVersion") or ""),
        "couponVersionId": str(detail.get("couponVersion") or ""),
        "projectType": detail.get("projectType"),
        "amount": amount_val,
    }
    if quote_id:
        order_ro["offerOrderId"] = quote_id
    project_id = str(args.get("project_id") or "").strip()
    sub_project_id = str(args.get("sub_project_id") or "").strip()
    project_id = _resolve_sub_project_for_order(client, sub_project_id, project_id, opportunity_id)
    if project_id:
        order_ro["projectId"] = project_id
    if sub_project_id:
        order_ro["subProjectId"] = sub_project_id
    for key in ("signerId", "signerName", "deptId", "deptName"):
        val = detail.get(key)
        if val not in (None, ""):
            order_ro[key] = val

    offer_save_ro: dict[str, Any] = {
        "offerOrderName": detail.get("offerOrderName"),
        "productId": str(detail.get("productId") or ""),
        "customerId": str(detail.get("customerId") or ""),
        "priceVersion": str(detail.get("priceVersion") or ""),
        "couponVersion": str(detail.get("couponVersion") or ""),
        "orderMoney": money_val,
        "factOrderMoney": amount_val,
        "orderType": detail.get("orderType") or 1,
        "projectType": detail.get("projectType"),
        "customerOpportunityId": opportunity_id,
        "contractId": detail.get("contractId"),
        "otherOffers": detail.get("otherOffers") or 0,
        "isCalculate": 1,
        "offerExtraChargeRoList": [],
        **{key: detail.get(key) for key in ("signerId", "signerName", "deptId", "deptName")},
        "itemRoList": [_order_item(item) for item in items],
    }
    if quote_id:
        offer_save_ro["id"] = quote_id
    for optional_key in (
        "preferentialTypeId",
        "preferentialReasons",
        "directReductionMoney",
        "directReductionRate",
    ):
        if detail.get(optional_key) not in (None, ""):
            offer_save_ro[optional_key] = detail[optional_key]

    payload: dict[str, Any] = {
        "submitMethod": 2,
        "orderRo": order_ro,
        "offerOrderSaveRo": offer_save_ro,
        "orderExtraChargeRoList": [],
    }
    if quote_id:
        payload["offerOrderId"] = quote_id
    return payload


def _load_quote_for_order(
    client: McnClient, quote_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    detail = client.get("/api/mcn/offerOrder/detail", {"id": quote_id})
    if not isinstance(detail, dict):
        raise McnClientError("报价单详情格式不正确")
    items = detail.get("offerOrderItemVos") or []
    if not items:
        raise McnClientError("报价单没有明细")
    return detail, items


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
    try:
        detail, items = _load_quote_for_order(client, quote_id)
    except McnClientError as exc:
        return f"下单失败：{exc}"
    payload = _build_order_payload(detail, args, items, client=client, quote_id=quote_id)
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
    try:
        detail, items = _load_quote_for_order(client, quote_id)
    except McnClientError as exc:
        return f"下单失败：{exc}"
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        key = _item_supplier(item) or "unassigned"
        groups.setdefault(key, []).append(item)
    prepared_groups: list[tuple[str, float, dict[str, Any]]] = []
    for supplier, group in groups.items():
        group_order_money = Decimal("0.00")
        group_fact_money = Decimal("0.00")
        for item in group:
            fact_val = _money(item.get("factMoney"))
            orig_val = _money(
                item.get("baseMoney")
                if item.get("baseMoney") is not None
                else item.get("couponAfterMoney")
                if item.get("couponAfterMoney") is not None
                else item.get("factMoney")
            )
            group_order_money += orig_val
            group_fact_money += fact_val
        order_num = _money_number(group_order_money)
        fact_num = _money_number(group_fact_money)
        payload = _build_order_payload(
            detail,
            args,
            group,
            client=client,
            quote_id=None,
            order_money=order_num,
            fact_order_money=fact_num,
        )
        prepared_groups.append((supplier, fact_num, payload))
    orders: list[dict[str, Any]] = []
    for supplier, amount, payload in prepared_groups:
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


def _update_publish_link(client: McnClient, args: dict[str, Any]) -> str:
    service_item_id = str(args.get("service_item_id") or "").strip()
    if not service_item_id:
        return "未保存：service_item_id 不能为空。"
    link = str(args.get("publish_link") or "").strip()
    if not link:
        return "未保存：发布链接不能为空。"
    if not bool(args.get("confirm")):
        return "未保存：请先让用户核对服务项目、账号规格和链接，确认后再以 confirm=true 调用。"
    existing_vo: dict[str, Any] = {}
    try:
        detail = client.post(
            "/api/mcn/order/service/item/editOrderServiceGetDetail",
            {"orderServiceItemId": service_item_id},
        )
        if isinstance(detail, dict) and isinstance(detail.get("orderServiceItemVo"), dict):
            existing_vo = detail["orderServiceItemVo"]
            if existing_vo.get("flowStatus") == 4:
                return "未保存：该服务项目流程已终止，不能编辑发布链接。"
            if existing_vo.get("status") == 1:
                return "未保存：该服务项目已完结，不允许编辑发布链接。"
    except McnClientError:
        existing_vo = {}
    payload: dict[str, Any] = {
        "id": service_item_id,
        "publishLink": link,
    }
    publish_time = _normalize_date(
        args.get("publish_time")
        if args.get("publish_time") not in (None, "")
        else existing_vo.get("publishTime"),
        "发布时间",
    )
    if publish_time:
        # MCN parses this field as a plain date; a time suffix makes it fail.
        payload["publishTime"] = publish_time
    publish_text = args.get("publish_text")
    if publish_text in (None, "") and existing_vo.get("publishText") not in (None, ""):
        publish_text = existing_vo.get("publishText")
    if publish_text not in (None, ""):
        payload["publishText"] = str(publish_text).strip()
    platform_order_id = args.get("platform_order_id")
    if platform_order_id in (None, "") and existing_vo.get("platformOrderId") not in (None, ""):
        platform_order_id = existing_vo.get("platformOrderId")
    if platform_order_id not in (None, ""):
        payload["platformOrderId"] = str(platform_order_id).strip()
    return _json(client.post("/api/mcn/order/service/item/edit", payload))
