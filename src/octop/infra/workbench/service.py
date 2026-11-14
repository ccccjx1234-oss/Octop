"""Domain service for the 会火 AI 工作台 (work progress, deliverables, data dashboard)."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from octop.infra.auth.sso.crypto import decrypt_secret
from octop.infra.auth.youli.mcn_client import McnClient
from octop.infra.db.repos.workbench import (
    WorkbenchDeliverableRow,
    WorkbenchGoalRow,
    WorkbenchTaskRow,
)
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.workbench.erp_source import (
    build_trend_series,
    fetch_erp_deliverables,
    fetch_role_slice,
    invalidate_user_erp_cache,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoleProfile:
    key: str
    label: str
    workspace_title: str
    target_type: str
    goal_tagline: str
    trend_metric_label: str
    modules: tuple[tuple[str, str], ...]
    goal_templates: tuple[tuple[str, float, str, str], ...]


WORKBENCH_ROLES: tuple[RoleProfile, ...] = (
    RoleProfile(
        key="business",
        label="商务",
        workspace_title="商务工作台",
        target_type="客户",
        goal_tagline="聚焦真实商机推进、报价转化与签约回款，数据直连 MCN ERP。",
        trend_metric_label="商单与报价数",
        modules=(("opportunity_follow", "商机跟进"), ("quote_contract", "报价与签约")),
        goal_templates=(
            ("订单完结率", 100.0, "%", "order_completion"),
            ("报价转订单推进", 10.0, "单", "offer_conversion"),
            ("合同签署数量", 5.0, "份", "contract_count"),
            ("回款完成率", 100.0, "%", "collection_rate"),
        ),
    ),
    RoleProfile(
        key="planning",
        label="策划",
        workspace_title="策划工作台",
        target_type="客户/账号",
        goal_tagline="联动 AI 创作热点、自营脚本库与全案子项目，持续沉淀可复用的内容资产。",
        trend_metric_label="脚本与立项数",
        modules=(("brief_strategy", "策略简报"), ("script_proposal", "方案与脚本")),
        goal_templates=(
            ("热点话题跟踪", 10.0, "个", "topic_track"),
            ("脚本库沉淀", 10.0, "条", "script_pool"),
            ("全案项目跟进", 5.0, "个", "project_plan"),
            ("对话交付审核通过", 5.0, "份", "deliverable_approved"),
        ),
    ),
    RoleProfile(
        key="customer_service",
        label="客户服务",
        workspace_title="客户服务工作台",
        target_type="客户",
        goal_tagline="实时同步 MCN 订单、报价与项目交付节点，确保每个客户合作透明可控。",
        trend_metric_label="订单与项目推进",
        modules=(("project_progress", "项目进度"), ("quote_order", "报价下单")),
        goal_templates=(
            ("订单完结进度", 10.0, "单", "order_close"),
            ("子项目转订单", 5.0, "个", "subproject_order"),
            ("待办清零推进", 10.0, "项", "pending_clear"),
            ("交付成果归档", 5.0, "份", "deliverable_total"),
        ),
    ),
    RoleProfile(
        key="mcn_ops",
        label="MCN运营",
        workspace_title="MCN运营工作台",
        target_type="达人/账号",
        goal_tagline="直连 MCN 达人排期、商单发布认领与 AI 创作内容，实时追踪履约匹配。",
        trend_metric_label="作品发布条数",
        modules=(("creator_schedule", "达人排期"), ("publish_tracking", "发布履约")),
        goal_templates=(
            ("商单发布匹配", 10.0, "条", "publish_match"),
            ("服务排期完成", 10.0, "项", "svc_exec"),
            ("自营脚本发布", 10.0, "条", "script_sync"),
            ("活跃达人账号", 10.0, "个", "account_active"),
        ),
    ),
    RoleProfile(
        key="editing",
        label="剪辑",
        workspace_title="剪辑工作台",
        target_type="成片项目",
        goal_tagline="对接 AI 创作脚本与 MCN 发布作品队列，跟踪成片输出与认领验收。",
        trend_metric_label="成片输出数",
        modules=(("rough_cut", "素材与初剪"), ("final_review", "精修交付")),
        goal_templates=(
            ("成片认领匹配", 10.0, "条", "pub_match"),
            ("脚本成片沉淀", 10.0, "条", "script_done"),
            ("交付物审核通过", 5.0, "份", "deliverable_approved"),
        ),
    ),
    RoleProfile(
        key="media",
        label="媒介",
        workspace_title="媒介工作台",
        target_type="媒介资源",
        goal_tagline="基于 MCN 账号资源池与供应商付款合同，透明跟进采买建单与排期。",
        trend_metric_label="采买与排期数",
        modules=(("talent_sourcing", "达人筛选"), ("media_buying", "采买下单")),
        goal_templates=(
            ("账号资源池", 10.0, "个", "resource_pool"),
            ("采买付款签约", 5.0, "份", "contract_bind"),
            ("交付清单归档", 5.0, "份", "deliverable_total"),
        ),
    ),
    RoleProfile(
        key="ad_buy",
        label="投流",
        workspace_title="投流工作台",
        target_type="投放计划",
        goal_tagline="直接读取 MCN 客户投放分布与定投/散投订单数据，实时观测投放体量。",
        trend_metric_label="投放结算额",
        modules=(("campaign_ops", "计划调优"), ("roi_review", "复盘报表")),
        goal_templates=(
            ("投放订单完结", 10.0, "单", "order_delivery"),
            ("复盘交付沉淀", 5.0, "份", "deliverable_total"),
        ),
    ),
    RoleProfile(
        key="finance",
        label="财务",
        workspace_title="财务工作台",
        target_type="结算单据",
        goal_tagline="同步 MCN 收款申请、对账单、开票与回款状态，确保结算数据真实可核。",
        trend_metric_label="财务单据笔数",
        modules=(("receivable_check", "应收对账"), ("payable_settlement", "应付结算")),
        goal_templates=(
            ("订单回款率", 100.0, "%", "collection_rate"),
            ("对账单核销", 5.0, "份", "recon_count"),
            ("财务交付归档", 5.0, "份", "deliverable_total"),
        ),
    ),
    RoleProfile(
        key="hr",
        label="人力",
        workspace_title="人力工作台",
        target_type="团队成员",
        goal_tagline="当前 MCN ERP 无独立人事接口，展示 Octop 协作任务与 AI 创作使用实绩。",
        trend_metric_label="协作与交付数",
        modules=(("hiring_pipeline", "招聘推进"), ("enablement", "团队赋能")),
        goal_templates=(
            ("交付成果沉淀", 5.0, "份", "deliverable_total"),
            ("任务按期完成", 5.0, "项", "task_done"),
        ),
    ),
)

_ROLE_MAP: dict[str, RoleProfile] = {profile.key: profile for profile in WORKBENCH_ROLES}

DELIVERABLE_TYPES: frozenset[str] = frozenset({"document", "link", "sheet", "ppt"})
REVIEW_STATUSES: frozenset[str] = frozenset({"pending_review", "approved", "needs_revision"})
TASK_STATUSES: frozenset[str] = frozenset({"pending", "in_progress", "pending_delivery", "done"})
_COHORT_RATIO_GOALS: frozenset[str] = frozenset(
    {
        "order_close",
        "subproject_order",
        "publish_match",
        "svc_exec",
        "pub_match",
        "order_delivery",
        "offer_conversion",
        "pending_clear",
    }
)


def _default_role(role_key: str | None) -> RoleProfile:
    if role_key and role_key in _ROLE_MAP:
        return _ROLE_MAP[role_key]
    return _ROLE_MAP["customer_service"]


_TZ_SH = ZoneInfo("Asia/Shanghai")


def _format_delivered_label(ts: int, *, now_ts: int | None = None) -> str:
    now_dt = datetime.fromtimestamp(now_ts if now_ts is not None else int(time.time()), tz=_TZ_SH)
    dt = datetime.fromtimestamp(ts, tz=_TZ_SH)
    hm = dt.strftime("%H:%M")
    if dt.date() == now_dt.date():
        return f"今天 {hm}"
    if dt.date() == (now_dt - timedelta(days=1)).date():
        return f"昨天 {hm}"
    return f"{dt.month}月{dt.day}日 {hm}"


def _task_to_dict(row: WorkbenchTaskRow) -> dict[str, Any]:
    source_labels = {"chat": "对话交付", "mcn": "MCN ERP", "manual": "手动创建"}
    return {
        "id": row.id,
        "role_key": row.role_key,
        "module_key": row.module_key,
        "module_title": row.module_title,
        "target_type": row.target_type,
        "target_name": row.target_name,
        "title": row.title,
        "due_label": row.due_label,
        "status": row.status,
        "progress": row.progress,
        "source": row.source,
        "source_label": source_labels.get(row.source, "本地任务"),
        "external_ref": row.external_ref,
        "readonly": False,
        "meta": {},
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _deliverable_to_dict(row: WorkbenchDeliverableRow) -> dict[str, Any]:
    src = "chat" if (row.source_message_id or row.source_thread_id) else "manual"
    return {
        "id": row.id,
        "role_key": row.role_key,
        "deliverable_type": row.deliverable_type,
        "target_name": row.target_name,
        "title": row.title,
        "summary": row.summary,
        "content": row.content,
        "review_status": row.review_status,
        "source_thread_id": row.source_thread_id,
        "source_message_id": row.source_message_id,
        "source": src,
        "source_label": "对话交付" if src == "chat" else "手动创建",
        "readonly": False,
        "external_url": "",
        "linked_task_id": row.linked_task_id,
        "delivered_at": row.delivered_at,
        "delivered_label": _format_delivered_label(row.delivered_at),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _goal_to_dict(row: WorkbenchGoalRow) -> dict[str, Any]:
    pct = 0
    if row.target_value > 0:
        pct = max(0, min(100, round((row.current_value / row.target_value) * 100)))
    return {
        "id": row.id,
        "role_key": row.role_key,
        "title": row.title,
        "target_value": row.target_value,
        "current_value": row.current_value,
        "unit": row.unit,
        "period": row.period,
        "percent": pct,
        "updated_at": row.updated_at,
    }


class WorkbenchService:
    def __init__(self, services: SharedServices) -> None:
        self._services = services
        self._repo = services.workbench_repo

    @staticmethod
    def roles_meta() -> list[dict[str, str]]:
        return [
            {
                "key": r.key,
                "label": r.label,
                "workspace_title": r.workspace_title,
                "target_type": r.target_type,
                "goal_tagline": r.goal_tagline,
            }
            for r in WORKBENCH_ROLES
        ]

    def _active_mcn_client(self, user_id: int) -> tuple[McnClient | None, bool, str]:
        try:
            sessions = self._services.youli_session_repo.list_active_for_user(user_id)
        except Exception:
            return None, False, ""
        now = int(time.time())
        active = next((s for s in sessions if s.active and s.abs_expires_at > now), None)
        if active is None or active.token_blob is None:
            return None, False, ""
        mcn_base = (
            getattr(self._services.config.youli, "mcn_base_url", "")
            or getattr(self._services.config.youli, "mcn_api_base", "")
            or ""
        )
        if not mcn_base:
            return None, False, "MCN 接口地址未配置"
        try:
            token = decrypt_secret(self._services.secret_repo, active.token_blob)
        except Exception as exc:
            logger.warning("failed decrypting youli token uid=%s: %s", user_id, exc)
            return None, False, "经贝凭证解密失败，请重新登录"
        return McnClient(self._services.config.youli, token, base_url=mcn_base), True, ""

    def _ensure_role_goals(self, user_id: int, role_key: str) -> list[WorkbenchGoalRow]:
        rows = self._repo.list_goals(user_id, role_key)
        if rows:
            return rows
        profile = _default_role(role_key)
        for idx, (title, target_value, unit, _) in enumerate(profile.goal_templates):
            self._repo.create_goal(
                user_id=user_id,
                role_key=profile.key,
                title=title,
                target_value=target_value,
                current_value=0.0,
                unit=unit,
                sort_order=idx,
            )
        return self._repo.list_goals(user_id, profile.key)

    def _sync_goals_from_real_metrics(
        self,
        user_id: int,
        profile: RoleProfile,
        rows: list[WorkbenchGoalRow],
        goal_progress: dict[str, tuple[float, float]],
        *,
        local_deliverables: list[WorkbenchDeliverableRow],
        local_tasks: list[WorkbenchTaskRow],
    ) -> list[WorkbenchGoalRow]:
        approved_cnt = float(sum(1 for d in local_deliverables if d.review_status == "approved"))
        total_deliv = float(len(local_deliverables))
        done_tasks = float(sum(1 for t in local_tasks if t.status == "done" or t.progress >= 100))

        local_metrics: dict[str, tuple[float, float]] = {
            "deliverable_approved": (approved_cnt, 5.0),
            "deliverable_total": (total_deliv, 5.0),
            "task_done": (done_tasks, 5.0),
        }

        updated_rows: list[WorkbenchGoalRow] = []
        for idx, row in enumerate(rows):
            template_entry = (
                profile.goal_templates[idx]
                if idx < len(profile.goal_templates)
                else None
            )
            metric_key = template_entry[3] if template_entry else ""
            default_target = template_entry[1] if template_entry else row.target_value
            pair = goal_progress.get(metric_key) or local_metrics.get(metric_key)
            if pair is not None:
                cur_val, suggested_target = pair
                # Only auto-sync target_value for cohort-ratio goals at their unmodified template default.
                should_sync_target = (
                    row.unit != "%"
                    and metric_key in _COHORT_RATIO_GOALS
                    and suggested_target > 0
                    and abs(row.target_value - default_target) <= 0.01
                    and abs(row.target_value - suggested_target) > 0.01
                )
                next_target = suggested_target if should_sync_target else row.target_value
                if abs(row.current_value - cur_val) > 0.01 or should_sync_target:
                    refreshed = self._repo.update_goal(
                        user_id,
                        row.id,
                        current_value=cur_val,
                        target_value=next_target if should_sync_target else None,
                    )
                    if refreshed is not None:
                        updated_rows.append(refreshed)
                        continue
            updated_rows.append(row)
        return updated_rows

    def get_progress(
        self,
        user_id: int,
        *,
        role_key: str | None = None,
        period_range: str = "week",
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        profile = _default_role(role_key)
        normalized_range = period_range if period_range in {"today", "week", "month"} else "week"
        local_tasks = self._repo.list_tasks(user_id, profile.key, include_seed=False)
        local_deliverables = self._repo.list_deliverables(user_id)

        client, has_session, client_err = self._active_mcn_client(user_id)
        erp_slice = fetch_role_slice(
            client,
            user_id,
            profile.key,
            "month" if normalized_range == "month" else "7d",
            force_refresh=force_refresh,
        )
        mcn_connected = bool(has_session and not erp_slice.auth_expired)
        mcn_error = erp_slice.error or client_err

        local_dicts = [_task_to_dict(row) for row in local_tasks]
        local_ext_refs = {it["external_ref"] for it in local_dicts if it.get("external_ref")}
        merged_items = list(local_dicts)
        for erp_item in erp_slice.tasks:
            if erp_item.get("external_ref") in local_ext_refs:
                continue
            merged_items.append(erp_item)

        now_ts = int(time.time())
        now_sh = datetime.fromtimestamp(now_ts, tz=_TZ_SH)
        today_start_ts = int(
            datetime(now_sh.year, now_sh.month, now_sh.day, tzinfo=_TZ_SH).timestamp()
        )
        if normalized_range == "today":
            period_cutoff = today_start_ts
        elif normalized_range == "month":
            period_cutoff = now_ts - 30 * 86400
        else:
            period_cutoff = now_ts - 7 * 86400

        def _item_ts(it: dict[str, Any]) -> int:
            return int(it.get("updated_at") or it.get("created_at") or 0)

        def _in_visible_range(it: dict[str, Any]) -> bool:
            is_done = it["status"] == "done" or int(it["progress"]) >= 100
            if not is_done or normalized_range == "month":
                return True
            ts = _item_ts(it)
            if normalized_range == "today":
                return ts >= today_start_ts or "今天" in str(it.get("due_label") or "")
            return ts >= period_cutoff

        visible_items = [it for it in merged_items if _in_visible_range(it)]

        delivered_recent = [
            d
            for d in local_deliverables
            if d.delivered_at >= period_cutoff
            and (d.role_key == profile.key or d.linked_task_id is not None)
        ]
        linked_done_ids = {
            d.linked_task_id for d in delivered_recent if d.linked_task_id is not None
        }

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        module_titles: dict[str, str] = dict(profile.modules)
        for item in visible_items:
            module_titles.setdefault(item["module_key"], item["module_title"])
            grouped[item["module_key"]].append(item)

        modules: list[dict[str, Any]] = []
        for mod_key, mod_title in module_titles.items():
            items = grouped.get(mod_key, [])
            modules.append(
                {
                    "module_key": mod_key,
                    "module_title": mod_title,
                    "target_type": profile.target_type,
                    "task_count": len(items),
                    "items": items,
                }
            )

        in_progress_count = sum(
            1
            for it in visible_items
            if it["status"] in {"in_progress", "pending_delivery", "pending"}
        )
        done_tasks_in_period = sum(
            1
            for it in visible_items
            if (it["status"] == "done" or int(it["progress"]) >= 100)
            and it["id"] not in linked_done_ids
            and (
                _item_ts(it) >= period_cutoff
                or (normalized_range == "today" and "今天" in str(it.get("due_label") or ""))
            )
        )
        avg_progress = (
            round(sum(int(it["progress"]) for it in visible_items) / len(visible_items))
            if visible_items
            else 0
        )

        return {
            "role": {
                "key": profile.key,
                "label": profile.label,
                "workspace_title": profile.workspace_title,
                "target_type": profile.target_type,
                "goal_tagline": profile.goal_tagline,
            },
            "roles": self.roles_meta(),
            "range": normalized_range,
            "mcn_connected": mcn_connected,
            "mcn_error": mcn_error,
            "data_sources": erp_slice.data_sources if mcn_connected else [],
            "kpis": {
                "core_work": len(visible_items),
                "in_progress": in_progress_count,
                "delivered_this_week": len(delivered_recent) + done_tasks_in_period,
                "overall_completion": avg_progress,
            },
            "modules": modules,
        }

    def create_task(self, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        profile = _default_role(str(payload.get("role_key") or "customer_service"))
        title = str(payload.get("title") or "").strip()
        if not title:
            raise OctopError(ErrorCode.BAD_REQUEST, "title is required")
        module_key = str(payload.get("module_key") or profile.modules[0][0]).strip()
        default_mod_title = dict(profile.modules).get(module_key, profile.modules[0][1])
        module_title = str(payload.get("module_title") or default_mod_title).strip()
        status = str(payload.get("status") or "in_progress").strip()
        if status not in TASK_STATUSES:
            status = "in_progress"
        progress = max(0, min(100, int(payload.get("progress") or 0)))
        if status == "done" and progress < 100:
            progress = 100
        row = self._repo.create_task(
            user_id=user_id,
            role_key=profile.key,
            module_key=module_key,
            module_title=module_title,
            target_type=str(payload.get("target_type") or profile.target_type).strip(),
            target_name=str(payload.get("target_name") or "").strip(),
            title=title,
            due_label=str(payload.get("due_label") or "本周内").strip(),
            status=status,
            progress=progress,
            source=str(payload.get("source") or "manual").strip() or "manual",
            external_ref=str(payload.get("external_ref") or "").strip(),
        )
        return _task_to_dict(row)

    def update_task(self, user_id: int, task_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        existing = self._repo.get_task(user_id, task_id)
        if existing is None:
            raise OctopError(ErrorCode.NOT_FOUND, "task not found")
        progress = payload.get("progress")
        parsed_progress: int | None = None
        if progress is not None:
            parsed_progress = max(0, min(100, int(progress)))
        status = payload.get("status")
        parsed_status: str | None = None
        if status is not None:
            cand = str(status).strip()
            if cand not in TASK_STATUSES:
                raise OctopError(ErrorCode.BAD_REQUEST, "invalid task status")
            parsed_status = cand
            if cand == "done" and parsed_progress is None:
                parsed_progress = 100
        updated = self._repo.update_task(
            user_id,
            task_id,
            title=str(payload["title"]).strip() if "title" in payload and payload["title"] is not None else None,
            target_name=(
                str(payload["target_name"]).strip()
                if "target_name" in payload and payload["target_name"] is not None
                else None
            ),
            due_label=(
                str(payload["due_label"]).strip()
                if "due_label" in payload and payload["due_label"] is not None
                else None
            ),
            status=parsed_status,
            progress=parsed_progress,
        )
        assert updated is not None
        return _task_to_dict(updated)

    def delete_task(self, user_id: int, task_id: int) -> None:
        if not self._repo.delete_task(user_id, task_id):
            raise OctopError(ErrorCode.NOT_FOUND, "task not found")

    def list_deliverables(
        self,
        user_id: int,
        *,
        deliverable_type: str = "all",
        query: str = "",
        review_status: str = "all",
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        local_rows = self._repo.list_deliverables(user_id)
        local_dicts = [_deliverable_to_dict(r) for r in local_rows]
        local_ext_ids = {
            r["source_message_id"] for r in local_dicts if r.get("source_message_id")
        }

        client, has_session, client_err = self._active_mcn_client(user_id)
        erp_items, auth_expired, erp_err = fetch_erp_deliverables(
            client, user_id, force_refresh=force_refresh
        )
        mcn_connected = bool(has_session and not auth_expired)
        mcn_error = erp_err or client_err

        combined = list(local_dicts)
        for item in erp_items:
            if item.get("source_message_id") in local_ext_ids:
                continue
            item["delivered_label"] = _format_delivered_label(int(item["delivered_at"]))
            combined.append(item)

        combined.sort(key=lambda x: int(x.get("delivered_at") or 0), reverse=True)

        week_cutoff = int(time.time()) - 7 * 86400
        kpis = {
            "total": len(combined),
            "pending_review": sum(1 for r in combined if r["review_status"] == "pending_review"),
            "approved": sum(1 for r in combined if r["review_status"] == "approved"),
            "added_this_week": sum(1 for r in combined if int(r["delivered_at"]) >= week_cutoff),
        }

        q = query.strip().lower()
        filtered: list[dict[str, Any]] = []
        for r in combined:
            if deliverable_type != "all" and r["deliverable_type"] != deliverable_type:
                continue
            if review_status != "all" and r["review_status"] != review_status:
                continue
            if (
                q
                and q not in str(r["target_name"]).lower()
                and q not in str(r["title"]).lower()
                and q not in str(r["summary"]).lower()
            ):
                continue
            filtered.append(r)

        return {
            "kpis": kpis,
            "items": filtered,
            "mcn_connected": mcn_connected,
            "mcn_error": mcn_error,
        }

    def create_deliverable(self, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title") or "").strip()
        if not title:
            raise OctopError(ErrorCode.BAD_REQUEST, "title is required")
        dtype = str(payload.get("deliverable_type") or "document").strip()
        if dtype not in DELIVERABLE_TYPES:
            raise OctopError(ErrorCode.BAD_REQUEST, "invalid deliverable_type")
        review_status = str(payload.get("review_status") or "pending_review").strip()
        if review_status not in REVIEW_STATUSES:
            raise OctopError(ErrorCode.BAD_REQUEST, "invalid review_status")
        role_key = _default_role(str(payload.get("role_key") or "customer_service")).key
        target_name = str(payload.get("target_name") or "").strip()
        content = str(payload.get("content") or "").strip()
        summary = str(payload.get("summary") or "").strip()
        if not summary and content:
            first_line = next((ln.strip() for ln in content.splitlines() if ln.strip()), "")
            summary = first_line[:96]
        source_thread_id = str(payload.get("source_thread_id") or "").strip()
        source_message_id = str(payload.get("source_message_id") or "").strip()

        existing = (
            self._repo.find_deliverable_by_source_message(user_id, source_message_id)
            if source_message_id
            else None
        )

        tasks = self._repo.list_tasks(user_id, role_key, include_seed=False)
        matched_task: WorkbenchTaskRow | None = None
        if existing and existing.linked_task_id is not None:
            matched_task = self._repo.get_task(user_id, existing.linked_task_id)
        if matched_task is None and target_name:
            for t in tasks:
                if t.target_name == target_name or target_name in t.target_name or t.target_name in target_name:
                    matched_task = t
                    break
        if matched_task is not None:
            next_progress = 100 if review_status == "approved" else min(100, max(matched_task.progress + 12, 85))
            next_status = "done" if next_progress >= 100 else "pending_delivery"
            matched_task = self._repo.update_task(
                user_id,
                matched_task.id,
                status=next_status,
                progress=next_progress,
            )
        else:
            profile = _default_role(role_key)
            init_progress = 100 if review_status == "approved" else 90
            matched_task = self._repo.create_task(
                user_id=user_id,
                role_key=profile.key,
                module_key=profile.modules[0][0],
                module_title=profile.modules[0][1],
                target_type=profile.target_type,
                target_name=target_name or "对话交付客户",
                title=f"交付成果：{title}",
                due_label="刚刚",
                status="done" if init_progress >= 100 else "pending_delivery",
                progress=init_progress,
                source="chat" if source_message_id else "manual",
            )

        if existing is not None:
            updated = self._repo.update_deliverable(
                user_id,
                existing.id,
                deliverable_type=dtype,
                target_name=target_name,
                title=title,
                summary=summary,
                content=content,
                review_status=review_status,
            )
            assert updated is not None
            return {
                "deliverable": _deliverable_to_dict(updated),
                "progress_synced": matched_task is not None,
                "updated_task": _task_to_dict(matched_task) if matched_task else None,
                "updated": True,
            }

        row = self._repo.create_deliverable(
            user_id=user_id,
            role_key=role_key,
            deliverable_type=dtype,
            target_name=target_name,
            title=title,
            summary=summary,
            content=content,
            review_status=review_status,
            source_thread_id=source_thread_id,
            source_message_id=source_message_id,
            linked_task_id=matched_task.id if matched_task else None,
        )
        return {
            "deliverable": _deliverable_to_dict(row),
            "progress_synced": matched_task is not None,
            "updated_task": _task_to_dict(matched_task) if matched_task else None,
            "updated": False,
        }

    def update_deliverable(
        self, user_id: int, deliverable_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        existing = self._repo.get_deliverable(user_id, deliverable_id)
        if existing is None:
            raise OctopError(ErrorCode.NOT_FOUND, "deliverable not found")
        dtype = payload.get("deliverable_type")
        if dtype is not None and str(dtype).strip() not in DELIVERABLE_TYPES:
            raise OctopError(ErrorCode.BAD_REQUEST, "invalid deliverable_type")
        review_status = payload.get("review_status")
        if review_status is not None and str(review_status).strip() not in REVIEW_STATUSES:
            raise OctopError(ErrorCode.BAD_REQUEST, "invalid review_status")
        updated = self._repo.update_deliverable(
            user_id,
            deliverable_id,
            deliverable_type=str(dtype).strip() if dtype is not None else None,
            target_name=(
                str(payload["target_name"]).strip()
                if "target_name" in payload and payload["target_name"] is not None
                else None
            ),
            title=(
                str(payload["title"]).strip()
                if "title" in payload and payload["title"] is not None
                else None
            ),
            summary=(
                str(payload["summary"]).strip()
                if "summary" in payload and payload["summary"] is not None
                else None
            ),
            content=(
                str(payload["content"]).strip()
                if "content" in payload and payload["content"] is not None
                else None
            ),
            review_status=str(review_status).strip() if review_status is not None else None,
        )
        assert updated is not None
        if updated.linked_task_id is not None and review_status is not None:
            new_progress = 100 if updated.review_status == "approved" else 75
            new_status = "done" if updated.review_status == "approved" else "in_progress"
            self._repo.update_task(
                user_id, updated.linked_task_id, status=new_status, progress=new_progress
            )
        return _deliverable_to_dict(updated)

    def delete_deliverable(self, user_id: int, deliverable_id: int) -> None:
        if not self._repo.delete_deliverable(user_id, deliverable_id):
            raise OctopError(ErrorCode.NOT_FOUND, "deliverable not found")

    def get_dashboard(
        self,
        user_id: int,
        *,
        role_key: str | None = None,
        period_range: str = "7d",
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        profile = _default_role(role_key)
        normalized_range = period_range if period_range in {"7d", "30d", "month"} else "7d"
        deliverables = self._repo.list_deliverables(user_id)
        local_tasks = self._repo.list_tasks(user_id, profile.key, include_seed=False)
        goal_rows = self._ensure_role_goals(user_id, profile.key)

        try:
            user_threads = self._services.thread_repo.list_for_user(user_id)
        except Exception:
            user_threads = []

        client, has_session, client_err = self._active_mcn_client(user_id)
        erp_slice = fetch_role_slice(
            client, user_id, profile.key, normalized_range, force_refresh=force_refresh
        )
        mcn_connected = bool(has_session and not erp_slice.auth_expired)
        mcn_error = erp_slice.error or client_err

        goal_rows = self._sync_goals_from_real_metrics(
            user_id,
            profile,
            goal_rows,
            erp_slice.goal_progress if mcn_connected else {},
            local_deliverables=deliverables,
            local_tasks=local_tasks,
        )

        if mcn_connected and erp_slice.kpis:
            kpis = erp_slice.kpis
            trend = erp_slice.trend
            data_sources = erp_slice.data_sources
        else:
            approved_deliv = sum(1 for d in deliverables if d.review_status == "approved")
            done_tasks = sum(1 for t in local_tasks if t.status == "done" or t.progress >= 100)
            kpis = [
                {
                    "label": "本角色本地任务",
                    "value": str(len(local_tasks)),
                    "delta": f"已完成 {done_tasks} 项",
                },
                {
                    "label": "沉淀交付成果",
                    "value": str(len(deliverables)),
                    "delta": f"审核通过 {approved_deliv} 份",
                },
                {
                    "label": "AI 协作会话",
                    "value": str(len(user_threads)),
                    "delta": "Octop 历史对话",
                },
                {
                    "label": "ERP 数据联动",
                    "value": "已连接" if mcn_connected else "未连接",
                    "delta": "无独立人事接口" if (mcn_connected and profile.key == "hr") else (mcn_error or "授权经贝员工后自动同步"),
                },
            ]
            local_pts = [(d.delivered_at, 1.0) for d in deliverables] + [
                (t.updated_at, 1.0) for t in local_tasks
            ]
            trend = build_trend_series(local_pts, normalized_range)
            data_sources = ["Octop 本地工作台"]

        return {
            "role": {
                "key": profile.key,
                "label": profile.label,
                "workspace_title": profile.workspace_title,
                "badge": f"{profile.label}实绩",
            },
            "roles": self.roles_meta(),
            "range": normalized_range,
            "mcn_connected": mcn_connected,
            "mcn_error": mcn_error,
            "data_sources": data_sources,
            "trend_metric_label": profile.trend_metric_label,
            "kpis": kpis,
            "trend": trend,
            "goals": [_goal_to_dict(g) for g in goal_rows],
        }

    def update_goal(self, user_id: int, goal_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        updated = self._repo.update_goal(
            user_id,
            goal_id,
            current_value=(
                float(payload["current_value"])
                if "current_value" in payload and payload["current_value"] is not None
                else None
            ),
            target_value=(
                float(payload["target_value"])
                if "target_value" in payload and payload["target_value"] is not None
                else None
            ),
        )
        if updated is None:
            raise OctopError(ErrorCode.NOT_FOUND, "goal not found")
        return _goal_to_dict(updated)

    def sync_mcn_orders(self, user_id: int, *, role_key: str = "customer_service") -> dict[str, Any]:
        """Force-refresh live ERP/AI Creator data for this user and clean legacy mock seed rows."""
        self._repo.purge_seed_tasks(user_id)
        invalidate_user_erp_cache(user_id)
        client, has_session, err = self._active_mcn_client(user_id)
        if client is None or not has_session:
            return {
                "synced": 0,
                "mcn_connected": False,
                "detail": err or "未检测到有效 MCN 会话，请用经贝员工账号登录",
            }
        profile = _default_role(role_key)
        try:
            erp_slice = fetch_role_slice(client, user_id, profile.key, "7d", force_refresh=True)
        except Exception as exc:
            logger.info("workbench MCN sync failed: %s", exc)
            return {"synced": 0, "mcn_connected": False, "detail": f"ERP 同步异常: {exc}"}

        if erp_slice.auth_expired:
            return {
                "synced": 0,
                "mcn_connected": False,
                "detail": erp_slice.error or "经贝授权已过期，请重新登录",
            }
        count = len(erp_slice.tasks)
        sources = "、".join(erp_slice.data_sources) if erp_slice.data_sources else "MCN ERP"
        return {
            "synced": count,
            "mcn_connected": True,
            "detail": f"已实时拉取 {count} 条{profile.label}业务数据（来源：{sources}）",
        }
