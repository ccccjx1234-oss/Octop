"""MCN CRM write handlers: projects, sub-projects, coupon versions, and contracts."""

from __future__ import annotations

from typing import Any

from octop.infra.auth.youli.mcn_client import McnClient, McnClientError
from octop.infra.auth.youli.pagination import post_all_pages

from .common import _json, _money, _money_number, _normalize_date, idempotent_post, payload_digest
from .quote import _find_opportunity


def _resolve_protect_type(
    client: McnClient,
    version_id: str,
    explicit_type: Any,
    list_path: str,
) -> int:
    """Resolve contract bind protectType (1=COMMON, 2=SPECIAL_DIRECT, 3=SPECIAL_COPY)."""
    if explicit_type not in (None, ""):
        val = int(explicit_type)
        if val not in (1, 2, 3):
            raise McnClientError("未创建：protectType 仅支持 1普通保价 2特客直签 3特客复签。")
        return val
    try:
        rows = post_all_pages(client, list_path, {}, page_size=100, max_pages=10)
    except McnClientError:
        return 1
    matched = next(
        (
            r
            for r in rows
            if isinstance(r, dict)
            and str(r.get("id")) == version_id
            and r.get("isSpecialCustomer") is not None
        ),
        None,
    )
    if matched and int(matched.get("isSpecialCustomer") or 0) == 1:
        return 2
    return 1


def _create_project(client: McnClient, args: dict[str, Any]) -> str:
    name = str(args.get("project_name") or "").strip()
    if not name:
        return "未创建：项目名称不能为空。"
    if not bool(args.get("confirm")):
        return "未创建：请先和用户确认项目名称，确认后再以 confirm=true 调用。"
    return _json(client.post("/api/mcn/customerProject/addProject", {"projectName": name}))


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
    try:
        matched_opp = _find_opportunity(
            client,
            opportunity_id=opportunity_id,
            multi_error=(
                "未创建：传入的 customer_opportunity_id 为客户主档 ID，该客户存在多个商机，"
                "请先调用 youli_list_opportunities 选择具体商机 ID。"
            ),
        )
    except McnClientError as exc:
        return str(exc)
    if matched_opp:
        opportunity_id = str(matched_opp["id"])
        if not customer_name:
            customer_name = str(
                matched_opp.get("name") or matched_opp.get("customerName") or ""
            ).strip()
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
    opportunity_id = str(args.get("opportunity_id") or "").strip()
    effective_time = _normalize_date(args.get("effective_time"), "生效日期")
    expiration_time = _normalize_date(args.get("expiration_time"), "失效日期")
    if effective_time and expiration_time and expiration_time < effective_time:
        return "未创建：失效日期不能早于生效日期。"
    payload: dict[str, Any] = {
        "name": name,
        "isDefault": 1 if bool(args.get("is_default")) else 0,
        "isSpecialCustomer": 1 if opportunity_id else 0,
        "isHide": 0,
        "lockStatus": 0,
    }
    version = str(args.get("version") or "").strip()
    if version:
        payload["version"] = version
    if opportunity_id:
        payload["opportunityId"] = opportunity_id
        opportunity_name = str(args.get("opportunity_name") or "").strip()
        if not opportunity_name:
            try:
                rows = post_all_pages(
                    client,
                    "/api/mcn/customer/opportunity/list",
                    {"useDataPermission": True},
                    page_size=100,
                    max_pages=10,
                )
                matched = next(
                    (r for r in rows if str(r.get("id")) == opportunity_id),
                    None,
                )
                if matched and matched.get("name"):
                    opportunity_name = str(matched["name"]).strip()
            except McnClientError:
                opportunity_name = ""
        if opportunity_name:
            payload["opportunityName"] = opportunity_name
    if effective_time:
        payload["effectiveTime"] = effective_time
    if expiration_time:
        payload["expirationTime"] = expiration_time
    return idempotent_post(
        client,
        f"coupon_ver:{payload_digest(payload)}",
        "/api/mcn/coupon/version/addCouponVersion",
        payload,
    )


def _apply_contract_protection_bind(
    client: McnClient,
    args: dict[str, Any],
    payload: dict[str, Any],
    *,
    version_id: str,
    effective_time: str,
    expiration_time: str,
    start_arg: str,
    end_arg: str,
    protect_arg: str,
    list_path: str,
    label: str,
    version_key: str,
    start_key: str,
    end_key: str,
    bind_list_key: str,
) -> str | None:
    start = _normalize_date(args.get(start_arg) or effective_time, start_arg)
    end = _normalize_date(args.get(end_arg) or expiration_time, end_arg)
    if end < start:
        return f"未创建：{label}结束时间不能早于开始时间。"
    if start < effective_time or end > expiration_time:
        return "未创建：保价格和保优惠时间都不能超出有效日期范围。"
    protect_type = _resolve_protect_type(client, version_id, args.get(protect_arg), list_path)
    payload[version_key] = version_id
    payload[start_key] = start
    payload[end_key] = end
    payload[bind_list_key] = [
        {
            "versionId": version_id,
            "protectType": protect_type,
            "startTime": start,
            "endTime": end,
        }
    ]
    return None


def _build_contract_payload(
    client: McnClient, args: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    contract_type = int(args.get("type") or 1)
    effective_time = _normalize_date(args.get("effective_time"), "effective_time", required=True)
    expiration_time = _normalize_date(args.get("expiration_time"), "expiration_time", required=True)
    if expiration_time < effective_time:
        return None, "未创建：expiration_time 不能早于 effective_time。"
    signing_date = _normalize_date(args.get("signing_date") or effective_time, "signing_date")
    cooperation_mode = int(args.get("cooperation_mode") or (1 if contract_type == 1 else 2))
    payload: dict[str, Any] = {
        "enableType": int(args.get("enable_type") or 2),
        "type": contract_type,
        "cooperationMode": cooperation_mode,
        "name": str(args.get("name") or "").strip(),
        "customerId": str(args.get("customer_id") or ""),
        "customerOpportunityId": str(args.get("customer_opportunity_id") or ""),
        "issuingOffice": str(args.get("issuing_office") or ""),
        "signingDate": signing_date,
        "effectiveTime": effective_time,
        "expirationTime": expiration_time,
        "orderNumber": 0,
        "orderAmount": 0,
        "amountReceived": 0,
        "pendingAmount": 0,
        "contractAccountInfo": args.get("contract_account_info") or {"periodType": 1},
    }
    if not payload["name"] or not payload["issuingOffice"]:
        return None, "未创建：name、issuing_office 不能为空。"
    if not payload["customerOpportunityId"]:
        return None, (
            "未创建：customer_opportunity_id 不能为空，否则合同不会出现在合同列表；"
            "请先调用 youli_list_opportunities 选择商机。"
        )
    if not payload["customerId"]:
        payload.pop("customerId", None)
    for arg_key, api_key in (
        ("employee_id", "employeeId"),
        ("user_name", "userName"),
        ("issuing_office_name", "issuingOfficeName"),
        ("remark", "remark"),
    ):
        val = str(args.get(arg_key) or "").strip()
        if val:
            payload[api_key] = val
    if args.get("scale_amount") not in (None, ""):
        payload["scaleAmount"] = _money_number(_money(args.get("scale_amount")))
    if args.get("guaranteed_type") not in (None, ""):
        payload["guaranteedType"] = int(args.get("guaranteed_type"))
    price_version = str(args.get("price_version_id") or "").strip()
    coupon_version = str(args.get("coupon_version_id") or "").strip()
    if price_version or coupon_version:
        payload["protectionPrice"] = 1
        if price_version:
            err = _apply_contract_protection_bind(
                client,
                args,
                payload,
                version_id=price_version,
                effective_time=effective_time,
                expiration_time=expiration_time,
                start_arg="price_start_time",
                end_arg="price_end_time",
                protect_arg="price_protect_type",
                list_path="/api/mcn/account/quote/version/list",
                label="保价格",
                version_key="priceVersion",
                start_key="priceStartTime",
                end_key="priceEndTime",
                bind_list_key="priceVersionBindList",
            )
            if err:
                return None, err
        if coupon_version:
            err = _apply_contract_protection_bind(
                client,
                args,
                payload,
                version_id=coupon_version,
                effective_time=effective_time,
                expiration_time=expiration_time,
                start_arg="favorable_start_time",
                end_arg="favorable_end_time",
                protect_arg="coupon_protect_type",
                list_path="/api/mcn/coupon/version/list",
                label="保优惠",
                version_key="couponVersion",
                start_key="favorableStartTime",
                end_key="favorableEndTime",
                bind_list_key="couponVersionBindList",
            )
            if err:
                return None, err
    else:
        payload["protectionPrice"] = 0
    return payload, None


def _create_contract(client: McnClient, args: dict[str, Any]) -> str:
    if not bool(args.get("confirm")):
        return (
            "未创建：请先和用户核对合同类型、客户、保价政策和有效期，确认后再以 confirm=true 调用。"
        )
    payload = args.get("payload")
    if payload is None:
        payload, err = _build_contract_payload(client, args)
        if err is not None:
            return err
    if not isinstance(payload, dict):
        return "未创建：payload 必须是合同对象"
    if not payload.get("enableType"):
        return "未创建：enableType 不能为空。"
    return idempotent_post(
        client,
        f"contract:{payload_digest(payload)}",
        "/api/mcn/contract/addContract",
        payload,
    )
