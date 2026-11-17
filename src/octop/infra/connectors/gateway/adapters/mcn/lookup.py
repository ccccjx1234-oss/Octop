"""Read-only MCN query handlers (accounts, quotes, customers, contracts, etc.)."""

from __future__ import annotations

from typing import Any

from octop.infra.auth.youli.mcn_client import McnClient
from octop.infra.auth.youli.pagination import parse_page_args

from .common import _json, _post_page, _slim


def _paged_query(
    client: McnClient,
    args: dict[str, Any],
    path: str,
    keys: tuple[str, ...],
    *,
    default_size: int = 20,
    extra_payload: dict[str, Any] | None = None,
    str_filters: tuple[tuple[str, str], ...] = (),
) -> str:
    page_num, page_size = parse_page_args(args, default_size=default_size, max_size=100)
    payload: dict[str, Any] = {"page": {"pageNum": page_num, "pageSize": page_size}}
    if extra_payload:
        payload.update(extra_payload)
    for arg_key, api_key in str_filters:
        val = str(args.get(arg_key) or "").strip()
        if val:
            payload[api_key] = val
    return _json(_post_page(client, path, payload, keys, page_num=page_num, page_size=page_size))


def _list_accounts(client: McnClient, args: dict[str, Any]) -> str:
    platform = int(args.get("platform") or 0)
    return _paged_query(
        client,
        args,
        "/api/mcn/account/list",
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
        extra_payload={"platforms": [platform]} if platform else None,
        str_filters=(("account_name", "accountName"),),
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
    return _json(client.post("/api/mcn/account/quotation/queryByProductSpecList", payload))


def _list_quotes(client: McnClient, args: dict[str, Any]) -> str:
    return _paged_query(
        client,
        args,
        "/api/mcn/offerOrder/list",
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
        default_size=10,
        str_filters=(("keyword", "offerOrderName"),),
    )


def _get_quote(client: McnClient, args: dict[str, Any]) -> str:
    quote_id = str(args.get("quote_id") or "").strip()
    if not quote_id:
        return "查询报价单详情失败：quote_id 不能为空"
    return _json(client.get("/api/mcn/offerOrder/detail", {"id": quote_id}))


def _list_products(client: McnClient, args: dict[str, Any]) -> str:
    return _paged_query(
        client,
        args,
        "/api/mcn/product/list",
        ("id", "name", "productType", "attribute", "productCategoryId"),
        str_filters=(("keyword", "name"),),
    )


def _list_customers(client: McnClient, args: dict[str, Any]) -> str:
    # The MCN list endpoint defaults this flag to false; list views must opt in.
    return _paged_query(
        client,
        args,
        "/api/mcn/customer-management/list",
        ("id", "name", "category", "contact", "mobile"),
        extra_payload={"useDataPermission": True},
        str_filters=(("keyword", "name"),),
    )


def _list_customer_ownerships(client: McnClient, args: dict[str, Any]) -> str:
    return _paged_query(
        client,
        args,
        "/api/mcn/customer/opportunity/list",
        (
            "id",
            "opportunityCode",
            "customerCode",
            "customerId",
            "name",
            "category",
            "contact",
            "job",
            "mobile",
            "type",
            "brandType",
            "isNewCustomer",
            "userName",
            "userTwoName",
            "createdTime",
        ),
        extra_payload={"useDataPermission": True},
        str_filters=(("keyword", "name"), ("customer_id", "customerId")),
    )


def _list_contracts(client: McnClient, args: dict[str, Any]) -> str:
    return _paged_query(
        client,
        args,
        "/api/mcn/contract/list",
        ("id", "name", "customerName", "status", "amount", "startTime", "endTime"),
        str_filters=(("keyword", "keyword"), ("customer_id", "customerId")),
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
    return _paged_query(
        client,
        args,
        "/api/mcn/customer/opportunity/list",
        ("id", "name", "customerId", "userName", "createdTime"),
        extra_payload={"useDataPermission": True},
        str_filters=(("keyword", "name"), ("customer_id", "customerId")),
    )


def _list_dictionaries(client: McnClient, args: dict[str, Any]) -> str:
    keys = [str(k).strip() for k in (args.get("enum_keys") or []) if str(k).strip()]
    if not keys:
        keys = ["project_type", "account_platform", "contract_type"]
    data = client.post("/api/mcn/enum/data/dictionary/getEnumList", {"enumKeys": keys})
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
    project_type = args.get("project_type")
    return _paged_query(
        client,
        args,
        "/api/mcn/customerSubProject/list",
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
        extra_payload={"projectTypeList": [int(project_type)]} if project_type else None,
        str_filters=(("keyword", "projectName"), ("customer_opportunity_id", "customerId")),
    )


def _list_price_versions(client: McnClient, args: dict[str, Any]) -> str:
    return _paged_query(
        client,
        args,
        "/api/mcn/account/quote/version/list",
        ("id", "name", "version", "isDefault", "status"),
        str_filters=(("keyword", "name"),),
    )


def _list_coupon_versions(client: McnClient, args: dict[str, Any]) -> str:
    return _paged_query(
        client,
        args,
        "/api/mcn/coupon/version/list",
        ("id", "name", "version", "isDefault", "status"),
        str_filters=(("keyword", "name"),),
    )


def _list_service_items(client: McnClient, args: dict[str, Any]) -> str:
    order_id = str(args.get("order_id") or "").strip()
    if not order_id:
        return "查询服务项目失败：order_id 不能为空"
    return _paged_query(
        client,
        args,
        "/api/mcn/order/service/item/page/list",
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
        extra_payload={"orderId": order_id},
    )
