"""Live MCN ERP and AI Creator data fetcher and mapper for the business workbench."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from octop.infra.auth.youli.mcn_client import (
    McnAuthExpired,
    McnClient,
    McnClientError,
    McnPermissionError,
)

logger = logging.getLogger(__name__)

_TZ_SH = timezone(timedelta(hours=8))
_CACHE_TTL_SECONDS = 90.0
_CACHE_MAX_ENTRIES = 512
_CACHE_LOCK = threading.Lock()
_ENDPOINT_CACHE: dict[tuple[int, str, str], tuple[float, Any]] = {}
_INFLIGHT: dict[tuple[int, str, str], _InflightCall] = {}


@dataclass
class _InflightCall:
    event: threading.Event
    value: Any = None
    exc: Exception | None = None

ORDER_STATUS_MAP: dict[int, tuple[str, int, str]] = {
    1: ("pending", 15, "待提交"),
    2: ("pending", 30, "待执行"),
    3: ("in_progress", 65, "执行中"),
    4: ("pending_delivery", 90, "已完成待完结"),
    5: ("pending_delivery", 92, "待开票"),
    6: ("pending_delivery", 95, "待回款"),
    7: ("done", 100, "已回款"),
    9: ("cancelled", 0, "已取消"),
    10: ("done", 100, "已完结"),
    11: ("done", 100, "已结束"),
}


def _prune_cache_locked(now: float) -> None:
    """Evict expired cache entries and cap in-memory cache size under ``_CACHE_LOCK``."""
    expired = [k for k, (ts, _) in _ENDPOINT_CACHE.items() if (now - ts) >= _CACHE_TTL_SECONDS]
    for k in expired:
        _ENDPOINT_CACHE.pop(k, None)
    overflow = len(_ENDPOINT_CACHE) - _CACHE_MAX_ENTRIES + 1
    if overflow > 0:
        oldest = sorted(_ENDPOINT_CACHE.items(), key=lambda kv: kv[1][0])[:overflow]
        for k, _ in oldest:
            _ENDPOINT_CACHE.pop(k, None)


@dataclass(frozen=True)
class ErpRoleSlice:
    tasks: list[dict[str, Any]]
    kpis: list[dict[str, str]]
    trend: list[dict[str, Any]]
    goal_progress: dict[str, tuple[float, float]]  # metric_key -> (current, target)
    data_sources: list[str]
    auth_expired: bool = False
    error: str = ""


def invalidate_user_erp_cache(user_id: int) -> None:
    with _CACHE_LOCK:
        stale = [key for key in _ENDPOINT_CACHE if key[0] == user_id]
        for key in stale:
            _ENDPOINT_CACHE.pop(key, None)


def parse_timestamp(val: Any) -> int | None:
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        num = float(val)
        if num > 10_000_000_000:
            num = num / 1000.0
        return int(num) if num > 0 else None
    text = str(val).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for candidate in (text, text.replace(" ", "T")):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_TZ_SH)
            return int(dt.timestamp())
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text[:19], fmt).replace(tzinfo=_TZ_SH)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None


def format_short_time(val: Any, fallback: str = "ERP 实时") -> str:
    ts = parse_timestamp(val)
    if ts is None:
        return fallback
    dt = datetime.fromtimestamp(ts, tz=_TZ_SH)
    return dt.strftime("%m-%d %H:%M") if dt.hour or dt.minute else dt.strftime("%m-%d")


def _extract_list(resp: Any) -> list[dict[str, Any]]:
    if isinstance(resp, list):
        return [item for item in resp if isinstance(item, dict)]
    if isinstance(resp, dict):
        for key in ("list", "records", "items", "rows", "topics", "conversations", "distributionList"):
            candidate = resp.get(key)
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
        inner = resp.get("data")
        if isinstance(inner, list):
            return [item for item in inner if isinstance(item, dict)]
        if isinstance(inner, dict):
            return _extract_list(inner)
    return []


def _extract_total(resp: Any) -> int:
    """Return paginated total when ERP response provides it, else extracted list size."""
    if isinstance(resp, dict):
        for container in (resp, resp.get("page")):
            if isinstance(container, dict):
                for key in ("total", "totalCount", "count"):
                    val = container.get(key)
                    if isinstance(val, (int, float)) and int(val) >= 0:
                        return int(val)
        inner = resp.get("data")
        if isinstance(inner, dict):
            return _extract_total(inner)
    return len(_extract_list(resp))


def _to_float(val: Any, default: float = 0.0) -> float:
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _fmt_money(amount: float) -> str:
    return f"¥ {amount:,.0f}"


def _fmt_rate(val: Any, suffix: str = "较上月") -> str:
    if val is None or val == "":
        return f"实时{suffix}"
    num = _to_float(val, 0.0)
    sign = "+" if num > 0 else ""
    return f"{sign}{num:.1f}% {suffix}" if suffix else f"{sign}{num:.1f}%"


def _order_status(raw: Any) -> tuple[str, int, str]:
    try:
        code = int(raw)
    except (TypeError, ValueError):
        return ("in_progress", 55, "推进中")
    return ORDER_STATUS_MAP.get(code, ("in_progress", 55, f"状态{code}"))


def _active_orders(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exclude cancelled (status=9) orders from execution progress and completion stats."""
    return [o for o in orders if _order_status(o.get("status"))[0] != "cancelled"]


def _offer_status(raw: Any) -> tuple[str, int]:
    """Map OfferOrderVo.status (0=未生成订单, 1=已生成订单) to workbench status/progress."""
    st_code = int(_to_float(raw, 0))
    return ("done", 100) if st_code >= 1 else ("in_progress", 45)


def _service_item_progress(raw: Any) -> tuple[str, int]:
    """Map MCN OrderServiceItemDo.progress (0..100 integer scale) to workbench status/progress."""
    pct = max(0, min(100, round(_to_float(raw, 0.0))))
    st = "done" if pct >= 100 else "in_progress" if pct > 0 else "pending"
    return st, pct


def build_trend_series(
    points: list[tuple[int | None, float]],
    period_range: str,
    *,
    now_ts: int | None = None,
) -> list[dict[str, Any]]:
    now_dt = datetime.fromtimestamp(now_ts or int(time.time()), tz=_TZ_SH)
    today = now_dt.date()

    if period_range == "7d":
        days = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
        buckets = dict.fromkeys(days, 0.0)
        for ts, weight in points:
            if ts is None:
                continue
            d = datetime.fromtimestamp(ts, tz=_TZ_SH).date()
            if d in buckets:
                buckets[d] += weight
        return [
            {
                "label": d.strftime("%m-%d"),
                "value": round(buckets[d], 2) if buckets[d] % 1 else int(buckets[d]),
            }
            for d in days
        ]

    if period_range == "month":
        first_day = today.replace(day=1)
        span_days = (today - first_day).days + 1
        step = max(1, (span_days + 5) // 6)
        slices: list[tuple[Any, Any, str]] = []
        cursor = first_day
        while cursor <= today:
            end_d = min(today, cursor + timedelta(days=step - 1))
            lbl = (
                f"{cursor.day}日"
                if cursor == end_d
                else f"{cursor.month}/{cursor.day}-{end_d.day}"
            )
            slices.append((cursor, end_d, lbl))
            cursor = end_d + timedelta(days=1)
    else:
        # 30d -> six 5-day windows
        slices = []
        for idx in range(5, -1, -1):
            end_d = today - timedelta(days=idx * 5)
            start_d = end_d - timedelta(days=4)
            lbl = f"{start_d.month}/{start_d.day}-{end_d.day}"
            slices.append((start_d, end_d, lbl))

    totals = [0.0] * len(slices)
    for ts, weight in points:
        if ts is None:
            continue
        d = datetime.fromtimestamp(ts, tz=_TZ_SH).date()
        for idx, (start_d, end_d, _) in enumerate(slices):
            if start_d <= d <= end_d:
                totals[idx] += weight
                break

    return [
        {
            "label": slices[idx][2],
            "value": round(totals[idx], 2) if totals[idx] % 1 else int(totals[idx]),
        }
        for idx in range(len(slices))
    ]


class ErpDataFetcher:
    """Thread-safe, per-user cached reader for MCN ERP and AI Creator endpoints."""

    def __init__(self, client: McnClient, user_id: int) -> None:
        self._client = client
        self._user_id = user_id
        self._token_fp = client.token_fingerprint

    def _call_one(
        self,
        key: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        force_refresh: bool = False,
    ) -> Any:
        cache_key = (self._user_id, self._token_fp, key)
        now = time.monotonic()
        waiter: _InflightCall | None = None
        is_leader = False

        with _CACHE_LOCK:
            if not force_refresh:
                cached = _ENDPOINT_CACHE.get(cache_key)
                if cached is not None and (now - cached[0]) < _CACHE_TTL_SECONDS:
                    return cached[1]
            existing = _INFLIGHT.get(cache_key)
            if existing is not None and not force_refresh:
                waiter = existing
            else:
                waiter = _InflightCall(event=threading.Event())
                if not force_refresh:
                    _INFLIGHT[cache_key] = waiter
                is_leader = True

        if not is_leader:
            waiter.event.wait(timeout=25.0)
            if waiter.exc is not None:
                raise waiter.exc
            if waiter.event.is_set():
                return waiter.value

        try:
            if method == "POST_META":
                res = self._client.post_with_meta(path, payload)
            elif method == "POST":
                res = self._client.post(path, payload)
            elif method == "GET_META":
                res = self._client.get_with_meta(path, payload)
            else:
                res = self._client.get(path)
            written_at = time.monotonic()
            with _CACHE_LOCK:
                _prune_cache_locked(written_at)
                _ENDPOINT_CACHE[cache_key] = (written_at, res)
            waiter.value = res
            return res
        except Exception as exc:
            waiter.exc = exc
            raise
        finally:
            if is_leader:
                with _CACHE_LOCK:
                    if _INFLIGHT.get(cache_key) is waiter:
                        _INFLIGHT.pop(cache_key, None)
                waiter.event.set()

    def fetch_bundle(
        self,
        specs: list[tuple[str, str, str, dict[str, Any] | None]],
        *,
        force_refresh: bool = False,
    ) -> tuple[dict[str, Any], bool, str]:
        results: dict[str, Any] = {}
        auth_expired = False
        first_error = ""
        has_perm_denied = False
        other_errors: list[str] = []

        def _worker(spec: tuple[str, str, str, dict[str, Any] | None]) -> tuple[str, Any, Exception | None]:
            key, method, path, payload = spec
            try:
                val = self._call_one(key, method, path, payload, force_refresh=force_refresh)
                return key, val, None
            except Exception as exc:  # noqa: BLE001
                return key, None, exc

        if len(specs) <= 1:
            outcomes = [_worker(s) for s in specs]
        else:
            with ThreadPoolExecutor(max_workers=min(8, len(specs))) as pool:
                outcomes = list(pool.map(_worker, specs))

        for key, val, exc in outcomes:
            if exc is None:
                results[key] = val
            elif isinstance(exc, McnAuthExpired):
                auth_expired = True
                first_error = str(exc) or "经贝授权已过期，请重新登录"
            elif isinstance(exc, McnPermissionError):
                logger.info("workbench erp call forbidden key=%s err=%s", key, exc)
                has_perm_denied = True
                results[key] = None
            else:
                logger.info("workbench erp call failed key=%s err=%s", key, exc)
                other_errors.append(str(exc))
                if not first_error and not isinstance(exc, McnClientError):
                    first_error = str(exc)
                results[key] = None

        if not auth_expired and not first_error:
            if outcomes and all(exc is not None for _, _, exc in outcomes):
                first_error = other_errors[0] if other_errors else "部分数据暂不可用"
            elif has_perm_denied:
                first_error = "部分 AI 创作接口仅内容数据管理员可见，已展示当前账号有权限的 MCN ERP 数据"
        return results, auth_expired, first_error


_PAGE_50 = {"page": {"pageNum": 1, "pageSize": 50}}

ROLE_ENDPOINTS: dict[str, list[tuple[str, str, str, dict[str, Any] | None]]] = {
    "business": [
        ("perf", "POST", "/api/mcn/report/workbench/performanceData", None),
        ("opportunities", "POST_META", "/api/mcn/customer/opportunity/list", _PAGE_50),
        ("offers", "POST_META", "/api/mcn/offerOrder/list", _PAGE_50),
        ("contracts", "POST_META", "/api/mcn/contract/list", _PAGE_50),
        ("orders", "POST_META", "/api/mcn/order/list", _PAGE_50),
    ],
    "planning": [
        ("topics", "GET", "/api/ai-creator/system-redfox-topics", None),
        ("sub_projects", "POST_META", "/api/mcn/customerSubProject/list", _PAGE_50),
        ("ai_scripts", "GET", "/api/ai-creator/owned-account-scripts", None),
        ("script_convs", "GET", "/api/ai-creator/script-conversations", None),
    ],
    "customer_service": [
        ("pending", "POST", "/api/mcn/report/workbench/pendingItems", None),
        ("service_progress", "POST", "/api/mcn/report/workbench/serviceProgress", None),
        ("sub_projects", "POST_META", "/api/mcn/customerSubProject/list", _PAGE_50),
        ("offers", "POST_META", "/api/mcn/offerOrder/list", _PAGE_50),
        ("orders", "POST_META", "/api/mcn/order/list", _PAGE_50),
    ],
    "mcn_ops": [
        ("pending", "POST", "/api/mcn/report/workbench/pendingItems", None),
        ("accounts", "POST_META", "/api/mcn/account/list", _PAGE_50),
        ("service_items", "POST_META", "/api/mcn/order/service/item/page/list", _PAGE_50),
        ("publishes", "POST_META", "/api/mcn/creator-work-publish/list", _PAGE_50),
        ("ai_scripts", "GET", "/api/ai-creator/owned-account-scripts", None),
    ],
    "editing": [
        ("ai_scripts", "GET", "/api/ai-creator/owned-account-scripts", None),
        ("service_items", "POST_META", "/api/mcn/order/service/item/page/list", _PAGE_50),
        ("publishes", "POST_META", "/api/mcn/creator-work-publish/list", _PAGE_50),
    ],
    "media": [
        ("accounts", "POST_META", "/api/mcn/account/list", _PAGE_50),
        ("payment_contracts", "POST_META", "/api/mcn/payment/contract/list", _PAGE_50),
        ("service_items", "POST_META", "/api/mcn/order/service/item/page/list", _PAGE_50),
    ],
    "ad_buy": [
        ("perf", "POST", "/api/mcn/report/workbench/performanceData", None),
        ("customer_dist", "POST", "/api/mcn/report/workbench/customerDistribution", None),
        ("orders", "POST_META", "/api/mcn/order/list", _PAGE_50),
    ],
    "finance": [
        ("pending", "POST", "/api/mcn/report/workbench/pendingItems", None),
        ("perf", "POST", "/api/mcn/report/workbench/performanceData", None),
        ("receipts", "POST_META", "/api/mcn/receipt/getReceiptRequestList", _PAGE_50),
        ("reconciliations", "POST_META", "/api/mcn/reconciliationManage/list", _PAGE_50),
        ("payment_contracts", "POST_META", "/api/mcn/payment/contract/list", _PAGE_50),
    ],
    "hr": [],
}

DELIVERABLE_ENDPOINTS: list[tuple[str, str, str, dict[str, Any] | None]] = [
    ("publishes", "POST_META", "/api/mcn/creator-work-publish/list", _PAGE_50),
    ("contracts", "POST_META", "/api/mcn/contract/list", _PAGE_50),
    ("ai_scripts", "GET", "/api/ai-creator/owned-account-scripts", None),
]


def _make_erp_task(
    *,
    role_key: str,
    module_key: str,
    module_title: str,
    target_type: str,
    target_name: str,
    title: str,
    due_label: str,
    status: str,
    progress: int,
    external_ref: str,
    source: str = "mcn",
    updated_at: int = 0,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": -abs(hash(external_ref)) % 1_000_000_000 - 1,
        "role_key": role_key,
        "module_key": module_key,
        "module_title": module_title,
        "target_type": target_type,
        "target_name": target_name or "MCN 业务",
        "title": title,
        "due_label": due_label,
        "status": status,
        "progress": max(0, min(100, int(progress))),
        "source": source,
        "source_label": "AI 创作" if source == "ai_creator" else "MCN ERP",
        "external_ref": external_ref,
        "readonly": True,
        "meta": meta or {},
        "created_at": updated_at,
        "updated_at": updated_at,
    }


def _map_business(bundle: dict[str, Any], period_range: str) -> ErpRoleSlice:
    perf = bundle.get("perf") if isinstance(bundle.get("perf"), dict) else {}
    opps = _extract_list(bundle.get("opportunities"))
    offers = _extract_list(bundle.get("offers"))
    contracts = _extract_list(bundle.get("contracts"))
    orders = _extract_list(bundle.get("orders"))

    tasks: list[dict[str, Any]] = []
    for o in opps[:6]:
        oid = str(o.get("id") or "")
        cname = str(o.get("name") or o.get("customerName") or "MCN 客户").strip()
        owner = str(o.get("userName") or "").strip()
        ts = parse_timestamp(o.get("updatedTime") or o.get("createdTime")) or 0
        tasks.append(
            _make_erp_task(
                role_key="business",
                module_key="opportunity_follow",
                module_title="商机跟进",
                target_type="客户",
                target_name=cname,
                title="跟进客户商机" + (f" · 负责人 {owner}" if owner else ""),
                due_label=format_short_time(o.get("updatedTime") or o.get("firstOrderDate")),
                status="in_progress" if o.get("firstOrderDate") else "pending",
                progress=75 if o.get("firstOrderDate") else 40,
                external_ref=f"mcn:opp:{oid}",
                updated_at=ts,
                meta={"type": "商机", "customerName": cname, "owner": owner, "firstOrderDate": o.get("firstOrderDate")},
            )
        )
    for of in offers[:4]:
        of_id = str(of.get("id") or of.get("offerOrderNo") or "")
        cname = str(of.get("customerName") or "MCN 客户").strip()
        oname = str(of.get("offerOrderName") or of.get("productName") or of.get("offerOrderNo") or "报价单").strip()
        status, pct = _offer_status(of.get("status"))
        ts = parse_timestamp(of.get("createdTime")) or 0
        tasks.append(
            _make_erp_task(
                role_key="business",
                module_key="quote_contract",
                module_title="报价与签约",
                target_type="客户",
                target_name=cname,
                title=f"[报价 {of.get('offerOrderNo') or ''}] {oname}",
                due_label=format_short_time(of.get("createdTime")),
                status=status,
                progress=pct,
                external_ref=f"mcn:offer:{of_id}",
                updated_at=ts,
                meta={"offerOrderNo": of.get("offerOrderNo"), "amount": of.get("factOrderMoney"), "creator": of.get("creatorName")},
            )
        )
    for c in contracts[:4]:
        cid = str(c.get("id") or c.get("contractNumber") or "")
        cname = str(c.get("customerName") or "MCN 客户").strip()
        cnum = str(c.get("contractNumber") or cid).strip()
        ctype = str(c.get("typeName") or "合同").strip()
        ts = parse_timestamp(c.get("updatedTime") or c.get("createdTime") or c.get("signingDate")) or 0
        tasks.append(
            _make_erp_task(
                role_key="business",
                module_key="quote_contract",
                module_title="报价与签约",
                target_type="客户",
                target_name=cname,
                title=f"[合同 {cnum}] {ctype} ({c.get('signingDate') or ''})",
                due_label=format_short_time(c.get("signingDate") or c.get("updatedTime")),
                status="done" if c.get("signingDate") else "pending_delivery",
                progress=100 if c.get("signingDate") else 70,
                external_ref=f"mcn:contract:{cid}",
                updated_at=ts,
                meta={"contractNumber": cnum, "typeName": ctype, "owner": c.get("userName")},
            )
        )

    order_amt = _to_float(perf.get("orderAmount"), 0.0)
    cust_num = int(_to_float(perf.get("customerNumber"), len(opps)))
    new_cust = int(_to_float(perf.get("newCustomerNumber"), 0))
    cust_price = _to_float(perf.get("customerPrice"), 0.0)
    recv_amt = _to_float(perf.get("receiveAmount"), 0.0)
    kpis = [
        {"label": "本月订单额", "value": _fmt_money(order_amt), "delta": _fmt_rate(perf.get("orderAmountRate"))},
        {"label": "活跃客户数", "value": str(cust_num), "delta": f"当月新客 {new_cust}"},
        {"label": "平均客单价", "value": _fmt_money(cust_price), "delta": _fmt_rate(perf.get("customerPriceRate"))},
        {"label": "本月已回款", "value": _fmt_money(recv_amt), "delta": _fmt_rate(perf.get("receiveAmountRate"))},
    ]

    valid_orders = _active_orders(orders)
    points: list[tuple[int | None, float]] = []
    for o in valid_orders:
        points.append((parse_timestamp(o.get("signerTime") or o.get("createdTime")), 1.0))
    for of in offers:
        points.append((parse_timestamp(of.get("createdTime")), 1.0))
    trend = build_trend_series(points, period_range)

    done_orders = sum(1 for o in valid_orders if _order_status(o.get("status"))[0] == "done")
    total_orders = len(valid_orders)
    orders_total = _extract_total(bundle.get("orders"))
    offers_total = _extract_total(bundle.get("offers"))
    contracts_total = _extract_total(bundle.get("contracts"))
    recv_rate = (recv_amt / order_amt * 100.0) if order_amt > 0 else 0.0
    order_done_pct = round(done_orders / total_orders * 100.0, 1) if total_orders else 0.0
    goal_progress = {
        "order_completion": (order_done_pct, 100.0),
        "offer_conversion": (float(orders_total), float(max(offers_total, 1))),
        "contract_count": (float(contracts_total), 5.0),
        "collection_rate": (round(min(100.0, recv_rate), 1), 100.0),
    }
    return ErpRoleSlice(
        tasks=tasks,
        kpis=kpis,
        trend=trend,
        goal_progress=goal_progress,
        data_sources=["MCN 商机", "MCN 报价/合同", "MCN 业绩报表"],
    )


def _map_customer_service(bundle: dict[str, Any], period_range: str) -> ErpRoleSlice:
    pending = bundle.get("pending") if isinstance(bundle.get("pending"), dict) else {}
    sp = bundle.get("service_progress") if isinstance(bundle.get("service_progress"), dict) else {}
    sub_projects = _extract_list(bundle.get("sub_projects"))
    offers = _extract_list(bundle.get("offers"))
    orders = _extract_list(bundle.get("orders"))

    tasks: list[dict[str, Any]] = []
    for p in sub_projects[:5]:
        pid = str(p.get("id") or p.get("subProjectCode") or "")
        cname = str(p.get("customerName") or "MCN 客户").strip()
        pname = str(p.get("projectName") or p.get("parentProjectName") or "子项目").strip()
        ts = parse_timestamp(p.get("createdTime")) or 0
        tasks.append(
            _make_erp_task(
                role_key="customer_service",
                module_key="project_progress",
                module_title="项目进度",
                target_type="客户",
                target_name=cname,
                title=f"[{p.get('subProjectCode') or '项目'}] {pname}",
                due_label=format_short_time(p.get("createdTime")),
                status="done" if p.get("hasOrder") else "in_progress",
                progress=90 if p.get("hasOrder") else 55,
                external_ref=f"mcn:subproj:{pid}",
                updated_at=ts,
                meta={"budget": p.get("projectBudget"), "owner": p.get("ownerName")},
            )
        )
    if len(tasks) < 5:
        for level_key in ("S1", "S2", "S3", "S4"):
            for item in (sp.get(level_key) or [])[:3]:
                if not isinstance(item, dict):
                    continue
                cname = str(item.get("customerName") or "MCN 客户").strip()
                st, pct, st_lbl = _order_status(item.get("status"))
                ts = parse_timestamp(item.get("createTime")) or 0
                tasks.append(
                    _make_erp_task(
                        role_key="customer_service",
                        module_key="project_progress",
                        module_title="项目进度",
                        target_type="客户",
                        target_name=cname,
                        title=f"[{level_key}服务] {st_lbl} · 结算 ¥{_to_float(item.get('settlementAmount')):,.0f}",
                        due_label=format_short_time(item.get("createTime")),
                        status=st,
                        progress=pct,
                        external_ref=f"mcn:sp:{level_key}:{cname}:{ts}",
                        updated_at=ts,
                        meta={"level": level_key, "settlementAmount": item.get("settlementAmount")},
                    )
                )

    valid_orders = _active_orders(orders)
    for o in valid_orders[:6]:
        oid = str(o.get("id") or o.get("orderNo") or "")
        cname = str(o.get("customerName") or "MCN 客户").strip()
        otitle = str(o.get("projectName") or o.get("productName") or o.get("contractName") or o.get("orderNo") or "").strip()
        st, pct, st_lbl = _order_status(o.get("status"))
        ts = parse_timestamp(o.get("signerTime") or o.get("createdTime")) or 0
        tasks.append(
            _make_erp_task(
                role_key="customer_service",
                module_key="quote_order",
                module_title="报价下单",
                target_type="客户",
                target_name=cname,
                title=f"[{o.get('orderNo') or '订单'}] {otitle} ({st_lbl})",
                due_label=format_short_time(o.get("signerTime") or o.get("createdTime")),
                status=st,
                progress=pct,
                external_ref=f"mcn:order:{oid}",
                updated_at=ts,
                meta={"orderNo": o.get("orderNo"), "amount": o.get("settlementAmount"), "statusLabel": st_lbl},
            )
        )
    for of in offers[:3]:
        of_id = str(of.get("id") or of.get("offerOrderNo") or "")
        cname = str(of.get("customerName") or "MCN 客户").strip()
        ts = parse_timestamp(of.get("createdTime")) or 0
        of_st, of_pct = _offer_status(of.get("status"))
        tasks.append(
            _make_erp_task(
                role_key="customer_service",
                module_key="quote_order",
                module_title="报价下单",
                target_type="客户",
                target_name=cname,
                title=f"[报价 {of.get('offerOrderNo') or ''}] {of.get('offerOrderName') or of.get('productName') or ''}",
                due_label=format_short_time(of.get("createdTime")),
                status=of_st,
                progress=of_pct,
                external_ref=f"mcn:offer:{of_id}",
                updated_at=ts,
                meta={"offerOrderNo": of.get("offerOrderNo"), "amount": of.get("factOrderMoney")},
            )
        )

    done_cnt = sum(1 for o in valid_orders if _order_status(o.get("status"))[0] == "done")
    total_cnt = len(valid_orders)
    orders_total = _extract_total(bundle.get("orders"))
    sub_projects_total = _extract_total(bundle.get("sub_projects"))
    wait_complete = int(_to_float(pending.get("waitComplete"), 0))
    complete_rate = round((done_cnt / total_cnt) * 100) if total_cnt else 0
    done_delta = (
        f"近{total_cnt}单有效订单已完成 {done_cnt} 单"
        if orders_total > total_cnt > 0
        else f"已完成 {done_cnt} 单"
    )
    kpis = [
        {"label": "MCN 订单数", "value": str(orders_total), "delta": f"子项目 {sub_projects_total} 个"},
        {"label": "待完结订单", "value": str(wait_complete), "delta": f"待接单 {int(_to_float(pending.get('waitPending'), 0))}"},
        {"label": "待开票 / 回款", "value": f"{int(_to_float(pending.get('waitInvoiced'), 0))} / {int(_to_float(pending.get('waitCollected'), 0))}", "delta": f"待认领 {int(_to_float(pending.get('waitClaimed'), 0))}"},
        {"label": "订单完结率", "value": f"{complete_rate}%", "delta": done_delta},
    ]
    pts = [
        (parse_timestamp(o.get("signerTime") or o.get("createdTime")), 1.0)
        for o in valid_orders
    ] + [(parse_timestamp(p.get("createdTime")), 1.0) for p in sub_projects]
    pending_denom = done_cnt + wait_complete
    goal_progress = {
        "order_close": (float(done_cnt), float(max(total_cnt, 1))),
        "subproject_order": (float(sum(1 for p in sub_projects if p.get("hasOrder"))), float(max(len(sub_projects), 1))),
        "pending_clear": (float(done_cnt), float(max(pending_denom, 1))) if pending_denom > 0 else (0.0, 10.0),
    }
    return ErpRoleSlice(
        tasks=tasks,
        kpis=kpis,
        trend=build_trend_series(pts, period_range),
        goal_progress=goal_progress,
        data_sources=["MCN 订单与报价", "MCN 项目进度", "MCN 工作台待办"],
    )


def _map_mcn_ops(bundle: dict[str, Any], period_range: str) -> ErpRoleSlice:
    pending = bundle.get("pending") if isinstance(bundle.get("pending"), dict) else {}
    svc_items = _extract_list(bundle.get("service_items"))
    publishes = _extract_list(bundle.get("publishes"))
    ai_scripts = _extract_list(bundle.get("ai_scripts"))

    tasks: list[dict[str, Any]] = []
    for it in svc_items[:6]:
        sid = str(it.get("id") or it.get("serviceNumber") or "")
        aname = str(it.get("accountName") or "达人账号").strip()
        st, pct = _service_item_progress(it.get("progress"))
        ts = parse_timestamp(it.get("updatedTime") or it.get("createdTime")) or 0
        tasks.append(
            _make_erp_task(
                role_key="mcn_ops",
                module_key="creator_schedule",
                module_title="达人排期",
                target_type="达人/账号",
                target_name=aname,
                title=f"[{it.get('serviceNumber') or '服务项'}] 关联订单 {it.get('orderNo') or '-'}",
                due_label=format_short_time(it.get("updatedTime") or it.get("createdTime")),
                status=st,
                progress=pct,
                external_ref=f"mcn:svc:{sid}",
                updated_at=ts,
                meta={"serviceNumber": it.get("serviceNumber"), "orderNo": it.get("orderNo")},
            )
        )
    for pub in publishes[:6]:
        cid = str(pub.get("claimId") or pub.get("serviceItemId") or "")
        aname = str(pub.get("accountName") or pub.get("claimantName") or "达人账号").strip()
        matched = bool(pub.get("matched")) or pub.get("matchStatus") == "MATCHED"
        ts = parse_timestamp(pub.get("publishTime") or pub.get("createdAt")) or 0
        tasks.append(
            _make_erp_task(
                role_key="mcn_ops",
                module_key="publish_tracking",
                module_title="发布履约",
                target_type="达人/账号",
                target_name=aname,
                title=f"[{pub.get('mediaPlatform') or '平台'}] {pub.get('projectName') or pub.get('orderNo') or '商单发布'}",
                due_label=format_short_time(pub.get("createdAt") or pub.get("publishTime")),
                status="done" if matched else "pending_delivery",
                progress=100 if matched else 75,
                external_ref=f"mcn:publish:{cid}",
                updated_at=ts,
                meta={"canonicalUrl": pub.get("canonicalUrl"), "orderNo": pub.get("orderNo")},
            )
        )

    accounts_total = _extract_total(bundle.get("accounts"))
    publishes_total = _extract_total(bundle.get("publishes"))
    svc_items_total = _extract_total(bundle.get("service_items"))
    ai_scripts_total = _extract_total(bundle.get("ai_scripts"))
    matched_cnt = sum(1 for p in publishes if bool(p.get("matched")) or p.get("matchStatus") == "MATCHED")
    kpis = [
        {"label": "签约账号数", "value": str(accounts_total), "delta": f"AI脚本 {ai_scripts_total} 条"},
        {"label": "商单发布数", "value": str(publishes_total), "delta": f"已匹配 {matched_cnt} 条"},
        {"label": "待认领作品", "value": str(int(_to_float(pending.get("waitClaimed"), 0))), "delta": f"待接单 {int(_to_float(pending.get('waitPending'), 0))}"},
        {"label": "履约匹配率", "value": f"{round(matched_cnt / len(publishes) * 100) if publishes else 0}%", "delta": f"服务排期 {svc_items_total} 项"},
    ]
    pts = [
        (parse_timestamp(p.get("createdAt") or p.get("publishTime")), 1.0)
        for p in publishes
    ] + [(parse_timestamp(s.get("publishTime")), 1.0) for s in ai_scripts]
    goal_progress = {
        "publish_match": (float(matched_cnt), float(max(len(publishes), 1))),
        "svc_exec": (float(sum(1 for it in svc_items if _service_item_progress(it.get("progress"))[1] >= 100)), float(max(len(svc_items), 1))),
        "script_sync": (float(ai_scripts_total), 10.0),
        "account_active": (float(accounts_total), 10.0),
    }
    return ErpRoleSlice(
        tasks=tasks,
        kpis=kpis,
        trend=build_trend_series(pts, period_range),
        goal_progress=goal_progress,
        data_sources=["MCN 账号与发布认领", "MCN 服务项", "AI 创作脚本"],
    )


def _map_planning(bundle: dict[str, Any], period_range: str) -> ErpRoleSlice:
    topics = _extract_list(bundle.get("topics"))
    sub_projects = _extract_list(bundle.get("sub_projects"))
    ai_scripts = _extract_list(bundle.get("ai_scripts"))

    tasks: list[dict[str, Any]] = []
    for tp in topics[:5]:
        tid = str(tp.get("id") or tp.get("keyword") or "")
        cat = str(tp.get("contentCategoryName") or tp.get("contentPrimaryCategory") or "热点").strip()
        ts = parse_timestamp(tp.get("lastFetchedAt")) or 0
        tasks.append(
            _make_erp_task(
                role_key="planning",
                module_key="brief_strategy",
                module_title="策略简报",
                target_type="客户/赛道",
                target_name=cat,
                title=f"[热点选题] {tp.get('keyword') or '趋势话题'}",
                due_label=format_short_time(tp.get("lastFetchedAt"), "AI 创作实时"),
                status="in_progress",
                progress=70,
                external_ref=f"ai:topic:{tid}",
                source="ai_creator",
                updated_at=ts,
                meta={"category": cat, "status": tp.get("status")},
            )
        )
    for sc in ai_scripts[:5]:
        sid = str(sc.get("id") or "")
        aname = str(sc.get("accountName") or "自营账号").strip()
        ts = parse_timestamp(sc.get("publishTime")) or 0
        tasks.append(
            _make_erp_task(
                role_key="planning",
                module_key="script_proposal",
                module_title="方案与脚本",
                target_type="客户/账号",
                target_name=aname,
                title=f"[{sc.get('platform') or '脚本'}] {sc.get('title') or '未命名脚本'}",
                due_label=format_short_time(sc.get("publishTime")),
                status="done" if sc.get("workUrl") else "in_progress",
                progress=100 if sc.get("workUrl") else 60,
                external_ref=f"ai:script:{sid}",
                source="ai_creator",
                updated_at=ts,
                meta={"workUrl": sc.get("workUrl"), "platform": sc.get("platform")},
            )
        )
    for sp in sub_projects[:3]:
        pid = str(sp.get("id") or "")
        ts = parse_timestamp(sp.get("createdTime")) or 0
        tasks.append(
            _make_erp_task(
                role_key="planning",
                module_key="brief_strategy",
                module_title="策略简报",
                target_type="客户",
                target_name=str(sp.get("customerName") or "MCN 客户").strip(),
                title=f"[全案立项] {sp.get('projectName') or ''}",
                due_label=format_short_time(sp.get("createdTime")),
                status="in_progress",
                progress=65,
                external_ref=f"mcn:subproj:{pid}",
                updated_at=ts,
            )
        )

    topics_total = _extract_total(bundle.get("topics"))
    sub_projects_total = _extract_total(bundle.get("sub_projects"))
    ai_scripts_total = _extract_total(bundle.get("ai_scripts"))
    script_convs_total = _extract_total(bundle.get("script_convs"))
    kpis = [
        {"label": "实时热点话题", "value": str(topics_total), "delta": "AI 创作热榜同步"},
        {"label": "全案/子项目", "value": str(sub_projects_total), "delta": "MCN 客户立项"},
        {"label": "自营脚本沉淀", "value": str(ai_scripts_total), "delta": f"脚本对话 {script_convs_total}"},
        {"label": "已发布脚本占比", "value": f"{round(sum(1 for s in ai_scripts if s.get('workUrl')) / len(ai_scripts) * 100) if ai_scripts else 0}%", "delta": "有发布链接校验"},
    ]
    pts = [(parse_timestamp(s.get("publishTime")), 1.0) for s in ai_scripts] + [
        (parse_timestamp(p.get("createdTime")), 1.0) for p in sub_projects
    ]
    goal_progress = {
        "topic_track": (float(topics_total), 10.0),
        "script_pool": (float(ai_scripts_total), 10.0),
        "project_plan": (float(sub_projects_total), 5.0),
    }
    return ErpRoleSlice(
        tasks=tasks,
        kpis=kpis,
        trend=build_trend_series(pts, period_range),
        goal_progress=goal_progress,
        data_sources=["AI 创作热点与脚本", "MCN 全案子项目"],
    )


def _map_editing(bundle: dict[str, Any], period_range: str) -> ErpRoleSlice:
    ai_scripts = _extract_list(bundle.get("ai_scripts"))
    publishes = _extract_list(bundle.get("publishes"))

    tasks: list[dict[str, Any]] = []
    for sc in ai_scripts[:6]:
        sid = str(sc.get("id") or "")
        ts = parse_timestamp(sc.get("publishTime")) or 0
        tasks.append(
            _make_erp_task(
                role_key="editing",
                module_key="rough_cut",
                module_title="素材与初剪",
                target_type="成片项目",
                target_name=str(sc.get("accountName") or "自营账号").strip(),
                title=f"[{sc.get('platform') or '视频'}] {sc.get('title') or ''}",
                due_label=format_short_time(sc.get("publishTime")),
                status="done" if sc.get("workUrl") else "in_progress",
                progress=100 if sc.get("workUrl") else 60,
                external_ref=f"ai:edit:{sid}",
                source="ai_creator",
                updated_at=ts,
            )
        )
    for pub in publishes[:6]:
        cid = str(pub.get("claimId") or pub.get("serviceItemId") or "")
        ts = parse_timestamp(pub.get("publishTime") or pub.get("createdAt")) or 0
        matched = bool(pub.get("matched")) or pub.get("matchStatus") == "MATCHED"
        tasks.append(
            _make_erp_task(
                role_key="editing",
                module_key="final_review",
                module_title="精修交付",
                target_type="成片项目",
                target_name=str(pub.get("projectName") or pub.get("accountName") or "商单成片").strip(),
                title=f"[{pub.get('mediaPlatform') or '作品'}] {pub.get('accountName') or pub.get('orderNo') or ''}",
                due_label=format_short_time(pub.get("createdAt")),
                status="done" if matched else "pending_delivery",
                progress=100 if matched else 85,
                external_ref=f"mcn:pub:{cid}",
                updated_at=ts,
            )
        )

    svc_items_total = _extract_total(bundle.get("service_items"))
    ai_scripts_total = _extract_total(bundle.get("ai_scripts"))
    publishes_total = _extract_total(bundle.get("publishes"))
    matched = sum(1 for p in publishes if bool(p.get("matched")) or p.get("matchStatus") == "MATCHED")
    kpis = [
        {"label": "执行服务项", "value": str(svc_items_total), "delta": "MCN 制作队列"},
        {"label": "成片/脚本库", "value": str(ai_scripts_total), "delta": "AI 创作脚本关联"},
        {"label": "已上线成片", "value": str(publishes_total), "delta": f"已认领匹配 {matched} 条"},
        {"label": "认领通过率", "value": f"{round(matched / len(publishes) * 100) if publishes else 0}%", "delta": "线上作品核验"},
    ]
    pts = [(parse_timestamp(p.get("createdAt")), 1.0) for p in publishes] + [
        (parse_timestamp(s.get("publishTime")), 1.0) for s in ai_scripts
    ]
    return ErpRoleSlice(
        tasks=tasks,
        kpis=kpis,
        trend=build_trend_series(pts, period_range),
        goal_progress={
            "pub_match": (float(matched), float(max(len(publishes), 1))),
            "script_done": (float(ai_scripts_total), 10.0),
        },
        data_sources=["MCN 商单发布", "MCN 服务项", "AI 创作脚本"],
    )


def _map_media(bundle: dict[str, Any], period_range: str) -> ErpRoleSlice:
    accounts = _extract_list(bundle.get("accounts"))
    pay_contracts = _extract_list(bundle.get("payment_contracts"))
    svc_items = _extract_list(bundle.get("service_items"))

    tasks: list[dict[str, Any]] = []
    for it in svc_items[:5]:
        sid = str(it.get("id") or it.get("serviceNumber") or "")
        aname = str(it.get("accountName") or "达人排期").strip()
        st, pct = _service_item_progress(it.get("progress"))
        ts = parse_timestamp(it.get("updatedTime") or it.get("createdTime")) or 0
        tasks.append(
            _make_erp_task(
                role_key="media",
                module_key="talent_sourcing",
                module_title="达人筛选",
                target_type="媒介资源",
                target_name=aname,
                title=f"[服务排期] {it.get('serviceNumber') or ''} · 订单 {it.get('orderNo') or '-'}",
                due_label=format_short_time(it.get("updatedTime") or it.get("createdTime")),
                status=st,
                progress=pct,
                external_ref=f"mcn:mediasvc:{sid}",
                updated_at=ts,
                meta={"serviceNumber": it.get("serviceNumber"), "orderNo": it.get("orderNo")},
            )
        )
    for ac in accounts[:4]:
        aid = str(ac.get("id") or ac.get("accountNumber") or "")
        aname = str(ac.get("accountName") or ac.get("accountNumber") or "达人账号").strip()
        grp = str(ac.get("distributionGroupName") or "默认分组").strip()
        tasks.append(
            _make_erp_task(
                role_key="media",
                module_key="talent_sourcing",
                module_title="达人筛选",
                target_type="媒介资源",
                target_name=aname,
                title=f"[{grp}] 账号 {ac.get('accountNumber') or ''}",
                due_label="资源库档案",
                status="in_progress",
                progress=50,
                external_ref=f"mcn:acc:{aid}",
            )
        )
    for pc in pay_contracts[:5]:
        pid = str(pc.get("id") or "")
        uname = str(pc.get("signingUserName") or pc.get("signingDeptName") or "供应商合同").strip()
        ts = parse_timestamp(pc.get("signingDate")) or 0
        signed = bool(pc.get("signingDate"))
        tasks.append(
            _make_erp_task(
                role_key="media",
                module_key="media_buying",
                module_title="采买下单",
                target_type="媒介资源",
                target_name=uname,
                title=f"[付款合同] {pc.get('signingDeptName') or ''} ({pc.get('signingDate') or ''})",
                due_label=format_short_time(pc.get("signingDate")),
                status="done" if signed else "in_progress",
                progress=100 if signed else 50,
                external_ref=f"mcn:paycontract:{pid}",
                updated_at=ts,
            )
        )

    accounts_total = _extract_total(bundle.get("accounts"))
    pay_contracts_total = _extract_total(bundle.get("payment_contracts"))
    svc_items_total = _extract_total(bundle.get("service_items"))
    kpis = [
        {"label": "媒介资源池", "value": str(accounts_total), "delta": "MCN 账号档案"},
        {"label": "采买付款合同", "value": str(pay_contracts_total), "delta": "达人/供应商协议"},
        {"label": "排期服务单", "value": str(svc_items_total), "delta": "进行中服务项"},
        {"label": "分组覆盖数", "value": str(len({a.get('distributionGroupName') for a in accounts if a.get('distributionGroupName')})), "delta": "资源分配组"},
    ]
    pts = [(parse_timestamp(pc.get("signingDate")), 1.0) for pc in pay_contracts] + [
        (parse_timestamp(it.get("updatedTime") or it.get("createdTime")), 1.0) for it in svc_items
    ]
    return ErpRoleSlice(
        tasks=tasks,
        kpis=kpis,
        trend=build_trend_series(pts, period_range),
        goal_progress={
            "resource_pool": (float(accounts_total), 10.0),
            "contract_bind": (float(pay_contracts_total), 5.0),
        },
        data_sources=["MCN 账号库", "MCN 付款合同", "MCN 服务排期"],
    )


def _map_ad_buy(bundle: dict[str, Any], period_range: str) -> ErpRoleSlice:
    perf = bundle.get("perf") if isinstance(bundle.get("perf"), dict) else {}
    dist = _extract_list(bundle.get("customer_dist"))
    valid_orders = _active_orders(_extract_list(bundle.get("orders")))

    # Filter to parentId == 0 top-level categories (matching MCN frontend) to avoid double-counting child rows.
    top_dist = [d for d in dist if d.get("parentId") in (None, "", 0, "0")] or dist
    total_dist_amt = sum(
        _to_float(d.get("settlementAmount") if d.get("settlementAmount") is not None else d.get("value"), 0.0)
        for d in top_dist
    )

    tasks: list[dict[str, Any]] = []
    for idx, d in enumerate(top_dist[:6]):
        cat = str(d.get("advertisingCategoryName") or d.get("parentName") or d.get("name") or "品类").strip()
        amt = _to_float(d.get("settlementAmount") if d.get("settlementAmount") is not None else d.get("value"), 0.0)
        share = (amt / total_dist_amt * 100.0) if total_dist_amt > 0 else (_to_float(d.get("rate"), 0.0) * 100.0)
        cat_id = d.get("advertisingCategoryId") or idx
        tasks.append(
            _make_erp_task(
                role_key="ad_buy",
                module_key="campaign_ops",
                module_title="计划调优",
                target_type="投放计划",
                target_name=cat,
                title=f"[行业投放] 结算 {_fmt_money(amt)} · 占比 {share:.1f}%",
                due_label="ERP 实时分布",
                status="in_progress",
                progress=50,
                external_ref=f"mcn:dist:{cat_id}:{cat}",
                meta={"categoryId": d.get("advertisingCategoryId"), "settlementAmount": amt, "sharePct": round(share, 1)},
            )
        )
    for o in valid_orders[:5]:
        oid = str(o.get("id") or o.get("orderNo") or "")
        cname = str(o.get("customerName") or "客户").strip()
        st, pct, st_lbl = _order_status(o.get("status"))
        ts = parse_timestamp(o.get("signerTime") or o.get("createdTime")) or 0
        tasks.append(
            _make_erp_task(
                role_key="ad_buy",
                module_key="roi_review",
                module_title="复盘报表",
                target_type="投放计划",
                target_name=cname,
                title=f"[{o.get('productName') or '投放'}] {o.get('orderNo') or ''} · {_fmt_money(_to_float(o.get('settlementAmount')))}",
                due_label=format_short_time(o.get("signerTime") or o.get("createdTime")),
                status=st,
                progress=pct,
                external_ref=f"mcn:adorder:{oid}",
                updated_at=ts,
            )
        )

    kpis = [
        {"label": "投放品类数", "value": str(len(top_dist)), "delta": f"总投放结算 {_fmt_money(total_dist_amt)}"},
        {"label": "接单金额", "value": _fmt_money(_to_float(perf.get("acceptAmount"), 0.0)), "delta": f"接单数 {int(_to_float(perf.get('acceptNumber'), 0))}"},
        {"label": "关联订单额", "value": _fmt_money(_to_float(perf.get("orderAmount"), 0.0)), "delta": _fmt_rate(perf.get("orderAmountRate"))},
        {"label": "平均客单", "value": _fmt_money(_to_float(perf.get("customerPrice"), 0.0)), "delta": _fmt_rate(perf.get("customerPriceRate"))},
    ]
    pts = [(parse_timestamp(o.get("signerTime") or o.get("createdTime")), _to_float(o.get("settlementAmount"), 1.0)) for o in valid_orders]
    return ErpRoleSlice(
        tasks=tasks,
        kpis=kpis,
        trend=build_trend_series(pts, period_range),
        goal_progress={
            "order_delivery": (float(sum(1 for o in valid_orders if _order_status(o.get("status"))[0] == "done")), float(max(len(valid_orders), 1))),
        },
        data_sources=["MCN 客户投放分布", "MCN 订单", "MCN 业绩报表"],
    )


def _map_finance(bundle: dict[str, Any], period_range: str) -> ErpRoleSlice:
    pending = bundle.get("pending") if isinstance(bundle.get("pending"), dict) else {}
    perf = bundle.get("perf") if isinstance(bundle.get("perf"), dict) else {}
    receipts = _extract_list(bundle.get("receipts"))
    recons = _extract_list(bundle.get("reconciliations"))

    tasks: list[dict[str, Any]] = []
    for rc in receipts[:6]:
        rid = str(rc.get("id") or rc.get("receiptCode") or "")
        cname = str(rc.get("customerName") or "MCN 客户").strip()
        money = _to_float(rc.get("money"), 0.0)
        done = int(_to_float(rc.get("status"), 0)) >= 1
        ts = parse_timestamp(rc.get("createTime") or rc.get("requestTime")) or 0
        tasks.append(
            _make_erp_task(
                role_key="finance",
                module_key="receivable_check",
                module_title="应收对账",
                target_type="结算单据",
                target_name=cname,
                title=f"[{rc.get('receiptCode') or '收款申请'}] {_fmt_money(money)}",
                due_label=format_short_time(rc.get("requestTime") or rc.get("createTime")),
                status="done" if done else "pending_delivery",
                progress=100 if done else 75,
                external_ref=f"mcn:receipt:{rid}",
                updated_at=ts,
            )
        )
    for rn in recons[:6]:
        rn_id = str(rn.get("id") or "")
        cname = str(rn.get("settlementBodyName") or "结算主体").strip()
        amt = _to_float(rn.get("amount"), 0.0)
        paid = int(_to_float(rn.get("payStatus"), 0)) >= 1
        ts = parse_timestamp(rn.get("createTime")) or 0
        tasks.append(
            _make_erp_task(
                role_key="finance",
                module_key="payable_settlement",
                module_title="应付结算",
                target_type="结算单据",
                target_name=cname,
                title=f"[对账单] {rn.get('name') or ''} · {_fmt_money(amt)}",
                due_label=format_short_time(rn.get("createTime")),
                status="done" if paid else "in_progress",
                progress=100 if paid else 60,
                external_ref=f"mcn:recon:{rn_id}",
                updated_at=ts,
            )
        )

    receipts_total = _extract_total(bundle.get("receipts"))
    pay_contracts_total = _extract_total(bundle.get("payment_contracts"))
    recons_total = _extract_total(bundle.get("reconciliations"))
    order_amt = _to_float(perf.get("orderAmount"), 0.0)
    recv_amt = _to_float(perf.get("receiveAmount"), 0.0)
    recv_rate = round(recv_amt / order_amt * 100, 1) if order_amt > 0 else 0.0
    kpis = [
        {"label": "待开票 / 待回款", "value": f"{int(_to_float(pending.get('waitInvoiced'), 0))} / {int(_to_float(pending.get('waitCollected'), 0))}", "delta": f"待认领 {int(_to_float(pending.get('waitClaimed'), 0))} 笔"},
        {"label": "本月已回款", "value": _fmt_money(recv_amt), "delta": _fmt_rate(perf.get("receiveAmountRate"))},
        {"label": "收款申请数", "value": str(receipts_total), "delta": f"应付合同 {pay_contracts_total} 份"},
        {"label": "订单回款率", "value": f"{min(100.0, recv_rate):.1f}%", "delta": f"对账单 {recons_total} 份"},
    ]
    pts = [(parse_timestamp(rc.get("createTime") or rc.get("requestTime")), 1.0) for rc in receipts] + [
        (parse_timestamp(rn.get("createTime")), 1.0) for rn in recons
    ]
    return ErpRoleSlice(
        tasks=tasks,
        kpis=kpis,
        trend=build_trend_series(pts, period_range),
        goal_progress={
            "collection_rate": (min(100.0, recv_rate), 100.0),
            "recon_count": (float(recons_total), 5.0),
        },
        data_sources=["MCN 收款申请", "MCN 对账管理", "MCN 业绩报表"],
    )


_ROLE_MAPPERS = {
    "business": _map_business,
    "planning": _map_planning,
    "customer_service": _map_customer_service,
    "mcn_ops": _map_mcn_ops,
    "editing": _map_editing,
    "media": _map_media,
    "ad_buy": _map_ad_buy,
    "finance": _map_finance,
}


def fetch_role_slice(
    client: McnClient | None,
    user_id: int,
    role_key: str,
    period_range: str,
    *,
    force_refresh: bool = False,
) -> ErpRoleSlice:
    if client is None or not client.enabled:
        return ErpRoleSlice(tasks=[], kpis=[], trend=[], goal_progress={}, data_sources=[])
    specs = ROLE_ENDPOINTS.get(role_key, [])
    if not specs:
        return ErpRoleSlice(
            tasks=[],
            kpis=[],
            trend=[],
            goal_progress={},
            data_sources=["Octop 本地协作"],
        )
    fetcher = ErpDataFetcher(client, user_id)
    bundle, auth_expired, err = fetcher.fetch_bundle(specs, force_refresh=force_refresh)
    if auth_expired:
        return ErpRoleSlice(
            tasks=[],
            kpis=[],
            trend=[],
            goal_progress={},
            data_sources=[],
            auth_expired=True,
            error=err,
        )
    mapper = _ROLE_MAPPERS.get(role_key)
    if mapper is None:
        return ErpRoleSlice(tasks=[], kpis=[], trend=[], goal_progress={}, data_sources=[])
    out = mapper(bundle, period_range)
    if err and not out.error:
        return ErpRoleSlice(
            tasks=out.tasks,
            kpis=out.kpis,
            trend=out.trend,
            goal_progress=out.goal_progress,
            data_sources=out.data_sources,
            error=err,
        )
    return out


def fetch_erp_deliverables(
    client: McnClient | None,
    user_id: int,
    *,
    force_refresh: bool = False,
) -> tuple[list[dict[str, Any]], bool, str]:
    """Pull real deliverables from MCN publish links, contracts, and AI Creator scripts."""
    if client is None or not client.enabled:
        return [], False, ""
    fetcher = ErpDataFetcher(client, user_id)
    bundle, auth_expired, err = fetcher.fetch_bundle(DELIVERABLE_ENDPOINTS, force_refresh=force_refresh)
    if auth_expired:
        return [], True, err

    items: list[dict[str, Any]] = []
    now_ts = int(time.time())

    for pub in _extract_list(bundle.get("publishes"))[:15]:
        cid = str(pub.get("claimId") or pub.get("serviceItemId") or "")
        url = str(pub.get("canonicalUrl") or pub.get("publishLink") or "").strip()
        acct = str(pub.get("accountName") or pub.get("claimantName") or "MCN 达人").strip()
        proj = str(pub.get("projectName") or pub.get("orderNo") or "商单发布").strip()
        platform = str(pub.get("mediaPlatform") or "平台").strip()
        matched = bool(pub.get("matched")) or pub.get("matchStatus") == "MATCHED"
        ts = parse_timestamp(pub.get("publishTime") or pub.get("createdAt")) or now_ts
        items.append(
            {
                "id": -100_000 - (abs(hash(f"pub:{cid}")) % 100_000),
                "role_key": "mcn_ops",
                "deliverable_type": "link",
                "target_name": proj,
                "title": f"[商单发布] {acct} · {platform}",
                "summary": f"{platform} 发布链接 · 订单 {pub.get('orderNo') or '-'} · {url}",
                "content": (
                    f"发布账号：{acct}\n"
                    f"平台：{platform}\n"
                    f"项目名称：{proj}\n"
                    f"订单号：{pub.get('orderNo') or '-'}\n"
                    f"服务单号：{pub.get('serviceNumber') or '-'}\n"
                    f"认领人：{pub.get('claimantName') or '-'}\n"
                    f"发布链接：{url}"
                ),
                "review_status": "approved" if matched else "pending_review",
                "source_thread_id": "",
                "source_message_id": f"mcn:publish:{cid}",
                "source": "mcn",
                "source_label": "MCN 发布认领",
                "readonly": True,
                "external_url": url,
                "linked_task_id": None,
                "delivered_at": ts,
                "created_at": ts,
                "updated_at": ts,
            }
        )

    for c in _extract_list(bundle.get("contracts"))[:10]:
        cid = str(c.get("id") or c.get("contractNumber") or "")
        cnum = str(c.get("contractNumber") or cid).strip()
        cname = str(c.get("customerName") or "MCN 客户").strip()
        ctype = str(c.get("typeName") or "合同").strip()
        ts = parse_timestamp(c.get("createdTime") or c.get("signingDate")) or now_ts
        file_path = str(c.get("filePath") or "").strip()
        items.append(
            {
                "id": -200_000 - (abs(hash(f"contract:{cid}")) % 100_000),
                "role_key": "business",
                "deliverable_type": "document",
                "target_name": cname,
                "title": f"[MCN 合同] {cnum} · {ctype}",
                "summary": f"签署日期 {c.get('signingDate') or '-'} · 有效期至 {c.get('expirationTime') or '-'} · 经办人 {c.get('userName') or '-'}",
                "content": (
                    f"合同编号：{cnum}\n"
                    f"客户：{cname}\n"
                    f"合同类型：{ctype}\n"
                    f"签署主体：{c.get('issuingOfficeName') or '-'}\n"
                    f"签署日期：{c.get('signingDate') or '-'}\n"
                    f"有效期：{c.get('effectiveTime') or '-'} ~ {c.get('expirationTime') or '-'}\n"
                    + (f"附件路径：{file_path}\n" if file_path else "")
                ),
                "review_status": "approved" if c.get("signingDate") else "pending_review",
                "source_thread_id": "",
                "source_message_id": f"mcn:contract:{cid}",
                "source": "mcn",
                "source_label": "MCN 合同档案",
                "readonly": True,
                "external_url": file_path if file_path.startswith("http") else "",
                "linked_task_id": None,
                "delivered_at": ts,
                "created_at": ts,
                "updated_at": ts,
            }
        )

    for sc in _extract_list(bundle.get("ai_scripts"))[:10]:
        sid = str(sc.get("id") or "")
        aname = str(sc.get("accountName") or "自营账号").strip()
        title = str(sc.get("title") or "未命名脚本").strip()
        platform = str(sc.get("platform") or "短视频").strip()
        url = str(sc.get("workUrl") or "").strip()
        ts = parse_timestamp(sc.get("publishTime")) or now_ts
        items.append(
            {
                "id": -300_000 - (abs(hash(f"aiscript:{sid}")) % 100_000),
                "role_key": "planning",
                "deliverable_type": "link" if url else "document",
                "target_name": aname,
                "title": f"[自营脚本] {title[:36]}",
                "summary": f"{platform} · {aname}" + (f" · {url}" if url else ""),
                "content": (
                    f"账号：{aname}\n"
                    f"平台：{platform}\n"
                    f"标题：{title}\n"
                    f"发布时间：{sc.get('publishTime') or '-'}\n"
                    + (f"作品链接：{url}\n" if url else "")
                ),
                "review_status": "approved" if url else "pending_review",
                "source_thread_id": "",
                "source_message_id": f"ai:script:{sid}",
                "source": "ai_creator",
                "source_label": "AI 创作脚本",
                "readonly": True,
                "external_url": url,
                "linked_task_id": None,
                "delivered_at": ts,
                "created_at": ts,
                "updated_at": ts,
            }
        )

    return items, False, err
