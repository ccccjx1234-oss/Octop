"""Unit tests for the business workbench API (progress, deliverables, dashboard, live ERP)."""

from __future__ import annotations

import dataclasses
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from octop.api.deps import current_user
from octop.api.routers.workbench import router as workbench_router
from octop.config import OctopConfig
from octop.infra.auth.sso.crypto import encrypt_secret
from octop.infra.auth.youli.mcn_client import McnAuthExpired, McnClient, McnPermissionError
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.users.identity import User
from octop.infra.utils.paths import PathLayout
from octop.infra.workbench.erp_source import invalidate_user_erp_cache


@pytest.fixture
def workbench_app(tmp_path):
    paths = PathLayout(tmp_path / ".octop")
    paths.ensure_root()
    db = SqlitePool(paths.db)
    run_migrations(db)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())
    uid1 = services.user_repo.create(
        username="alice",
        password_hash="x",
        role="user",
    )
    uid2 = services.user_repo.create(
        username="bob",
        password_hash="x",
        role="user",
    )
    state = {"user": User(id=uid1, username="alice", role="user", display_name="Alice", permissions=[])}
    users = {
        uid1: state["user"],
        uid2: User(id=uid2, username="bob", role="user", display_name="Bob", permissions=[]),
    }

    app = FastAPI()
    app.state.octop_server = SimpleNamespace(services=services, _started=True)
    app.include_router(workbench_router, prefix="/api")
    app.dependency_overrides[current_user] = lambda: state["user"]
    return app, state, users


@pytest.mark.asyncio
async def test_workbench_unconnected_has_zero_mock_seed_data(workbench_app):
    app, _, _ = workbench_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        progress = (
            await client.get("/api/workbench/progress", params={"role": "customer_service"})
        ).json()
        deliverables = (await client.get("/api/workbench/deliverables")).json()
        sync_res = (
            await client.post("/api/workbench/sync-mcn", json={"role_key": "customer_service"})
        ).json()

    assert len(progress["roles"]) == 9
    assert progress["role"]["key"] == "customer_service"
    assert [m["module_title"] for m in progress["modules"]] == ["项目进度", "报价下单"]
    assert progress["mcn_connected"] is False
    assert progress["kpis"]["core_work"] == 0
    assert progress["kpis"]["overall_completion"] == 0
    assert all(m["items"] == [] for m in progress["modules"])
    assert deliverables["items"] == []
    assert deliverables["mcn_connected"] is False
    assert sync_res["mcn_connected"] is False
    assert sync_res["synced"] == 0


@pytest.mark.asyncio
async def test_chat_deliverable_upsert_syncs_progress_and_isolates_users(workbench_app):
    app, state, users = workbench_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/workbench/deliverables",
            json={
                "role_key": "customer_service",
                "deliverable_type": "document",
                "target_name": "青禾科技",
                "title": "青禾科技合作方案（V1.0）",
                "content": "交付目标：围绕客户当前需求整理可直接执行的合作方案。",
                "review_status": "pending_review",
                "source_thread_id": "thread-1",
                "source_message_id": "msg-1",
            },
        )
        assert created.status_code == 200
        payload = created.json()
        assert payload["updated"] is False
        assert payload["progress_synced"] is True
        deliv_id = payload["deliverable"]["id"]
        task_id = payload["updated_task"]["id"]

        # Re-saving the same message updates the existing record instead of duplicating
        updated_again = await client.post(
            "/api/workbench/deliverables",
            json={
                "role_key": "customer_service",
                "deliverable_type": "document",
                "target_name": "青禾科技",
                "title": "青禾科技合作方案（V1.1）",
                "content": "修订后方案正文",
                "review_status": "approved",
                "source_thread_id": "thread-1",
                "source_message_id": "msg-1",
            },
        )
        assert updated_again.status_code == 200
        again_payload = updated_again.json()
        assert again_payload["updated"] is True
        assert again_payload["deliverable"]["id"] == deliv_id
        assert again_payload["deliverable"]["title"] == "青禾科技合作方案（V1.1）"

        listed = (await client.get("/api/workbench/deliverables", params={"q": "青禾科技"})).json()
        matching = [item for item in listed["items"] if item["source_message_id"] == "msg-1"]
        assert len(matching) == 1

        after = (
            await client.get("/api/workbench/progress", params={"role": "customer_service"})
        ).json()
        updated_task = next(
            it for m in after["modules"] for it in m["items"] if it["id"] == task_id
        )
        assert updated_task["progress"] == 100
        assert updated_task["status"] == "done"

        # Switch to user 2 — user 1's deliverable must not appear
        uid2 = [k for k in users if k != state["user"].id][0]
        state["user"] = users[uid2]
        user2_list = (
            await client.get("/api/workbench/deliverables", params={"q": "青禾科技"})
        ).json()
        assert user2_list["items"] == []


@pytest.mark.asyncio
async def test_workbench_dashboard_range_and_goal_update(workbench_app):
    app, _, _ = workbench_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post(
            "/api/workbench/tasks",
            json={
                "role_key": "business",
                "module_key": "opportunity_follow",
                "target_name": "测试客户A",
                "title": "跟进报价",
                "status": "done",
                "progress": 100,
            },
        )
        dash_7d = (
            await client.get("/api/workbench/dashboard", params={"role": "business", "range": "7d"})
        ).json()
        dash_30d = (
            await client.get(
                "/api/workbench/dashboard", params={"role": "business", "range": "30d"}
            )
        ).json()

        assert dash_7d["role"]["label"] == "商务"
        assert len(dash_7d["kpis"]) == 4
        assert len(dash_7d["trend"]) == 7
        assert len(dash_30d["trend"]) == 6
        assert len(dash_7d["goals"]) == 4

        first_goal_id = dash_7d["goals"][0]["id"]
        patched = (
            await client.patch(
                f"/api/workbench/goals/{first_goal_id}",
                json={"target_value": 80},
            )
        ).json()
        assert patched["target_value"] == 80.0


@pytest.mark.asyncio
async def test_workbench_live_mcn_erp_integration_and_expiry(workbench_app, monkeypatch):
    app, state, _ = workbench_app
    services = app.state.octop_server.services
    monkeypatch.setattr(
        app.state.octop_server,
        "services",
        dataclasses.replace(
            services,
            config=dataclasses.replace(
                services.config,
                youli=dataclasses.replace(
                    services.config.youli, mcn_base_url="https://mcn.example"
                ),
            ),
        ),
    )
    uid = state["user"].id
    invalidate_user_erp_cache(uid)
    now = int(time.time())
    services.youli_session_repo.create(
        sid="sid-sync-1",
        user_id=uid,
        account_user_id=101,
        account_username="15000000088",
        company_id=147372,
        employee_id=2823591,
        industry=None,
        token_blob=encrypt_secret(services.secret_repo, "mcn-token-123"),
        token_expires_at=now + 3600,
        abs_expires_at=now + 7200,
    )

    today_str = time.strftime("%Y-%m-%d 10:00:00", time.localtime(now))
    called: list[str] = []

    def fake_post(self, path, payload=None):
        called.append(path)
        if path == "/api/mcn/report/workbench/pendingItems":
            return {"waitComplete": 4, "waitInvoice": 2, "waitReceipt": 1, "waitOffer": 3}
        if path == "/api/mcn/report/workbench/performanceData":
            return {
                "orderAmount": 1327353.3,
                "orderProfit": 420000,
                "orderProfitRate": "31.64%",
                "orderCount": 12,
                "orderReceiptAmount": 500000,
                "customerNumber": 5,
            }
        if path == "/api/mcn/report/workbench/customerStatistics":
            return {"customerNumber": 5, "cooperationCount": 8, "newCustomerCount": 2}
        if path == "/api/mcn/report/workbench/customerDistribution":
            return {"distributionList": [{"name": "定投", "value": 800000}]}
        if path == "/api/mcn/report/workbench/serviceProgress":
            return {"serviceOrderCount": 6, "completeRate": "83%"}
        if path == "/api/mcn/report/workbench/accountData":
            return {"accountCount": 9, "publishCount": 6}
        if path == "/api/mcn/customerSubProject/list":
            return {
                "records": [
                    {
                        "id": 201,
                        "name": "超级飞侠十周年传播",
                        "customerName": "超级飞侠",
                        "projectStatus": 2,
                        "updateTime": today_str,
                    }
                ]
            }
        if path == "/api/mcn/offerOrder/list":
            return {
                "records": [
                    {
                        "id": 301,
                        "offerNo": "OFF-301",
                        "customerName": "茶颜",
                        "projectName": "茶颜双十一达人报价",
                        "offerPrice": 68000,
                        "status": 1,
                        "createTime": today_str,
                    }
                ]
            }
        if path == "/api/mcn/order/list":
            return {
                "records": [
                    {
                        "id": 9001,
                        "orderNo": "ORD-9001",
                        "customerName": "茶颜",
                        "projectName": "茶颜双十一投放执行单",
                        "status": 3,
                        "createTime": today_str,
                    }
                ]
            }
        if path == "/api/mcn/creator-work-publish/list":
            return {
                "records": [
                    {
                        "claimId": 501,
                        "projectName": "茶颜开箱测评",
                        "accountName": "测试达人号",
                        "mediaPlatform": "抖音",
                        "canonicalUrl": "https://example.com/v/501",
                        "matched": True,
                        "publishTime": today_str,
                    }
                ]
            }
        if path == "/api/mcn/contract/list":
            return {
                "records": [
                    {
                        "id": 601,
                        "contractNumber": "HT202611130120",
                        "customerName": "茶颜",
                        "typeName": "年度框架合同",
                        "signingDate": today_str,
                        "createdTime": today_str,
                    }
                ]
            }
        return {"records": []}

    def fake_get(self, path, params=None):
        called.append(path)
        if path == "/api/ai-creator/owned-account-scripts":
            return {
                "records": [
                    {
                        "id": 701,
                        "title": "冬日热饮短视频口播脚本",
                        "accountName": "经贝官方账号",
                        "status": "approved",
                        "updateTime": today_str,
                    }
                ]
            }
        return {"records": []}

    monkeypatch.setattr(McnClient, "post", fake_post)
    monkeypatch.setattr(McnClient, "get", fake_get)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        sync_res = (
            await client.post("/api/workbench/sync-mcn", json={"role_key": "customer_service"})
        ).json()
        progress = (
            await client.get("/api/workbench/progress", params={"role": "customer_service"})
        ).json()
        deliverables = (await client.get("/api/workbench/deliverables")).json()
        dashboard = (
            await client.get("/api/workbench/dashboard", params={"role": "business", "range": "7d"})
        ).json()

    assert sync_res["mcn_connected"] is True
    assert sync_res["synced"] >= 2
    assert progress["mcn_connected"] is True
    items = [item for mod in progress["modules"] for item in mod["items"]]
    assert any(it["target_name"] == "茶颜" and it["readonly"] is True for it in items)
    assert any(it["target_name"] == "超级飞侠" and it["source"] == "mcn" for it in items)
    assert deliverables["mcn_connected"] is True
    assert any(d["source"] == "mcn" and d["external_url"] == "https://example.com/v/501" for d in deliverables["items"])
    assert any(d["source"] == "ai_creator" for d in deliverables["items"])
    assert dashboard["mcn_connected"] is True
    assert any("1,327,353" in k["value"] for k in dashboard["kpis"])
    assert "/api/mcn/order/list" in called

    # Verify McnAuthExpired degrades cleanly without throwing HTTP 500
    invalidate_user_erp_cache(uid)

    def expired_post(self, path, payload=None):
        raise McnAuthExpired("token expired")

    monkeypatch.setattr(McnClient, "post", expired_post)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        expired_progress = await client.get(
            "/api/workbench/progress", params={"role": "customer_service"}
        )
    assert expired_progress.status_code == 200
    expired_body = expired_progress.json()
    assert expired_body["mcn_connected"] is False
    assert expired_body["mcn_error"]


def test_format_delivered_label_uses_shanghai_tz() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from octop.infra.workbench.erp_source import _extract_total
    from octop.infra.workbench.service import _format_delivered_label

    sh = ZoneInfo("Asia/Shanghai")
    now_ts = int(datetime(2026, 11, 14, 15, 0, tzinfo=sh).timestamp())
    today_ts = int(datetime(2026, 11, 14, 12, 18, tzinfo=sh).timestamp())
    yesterday_ts = int(datetime(2026, 11, 13, 18, 17, tzinfo=sh).timestamp())
    older_ts = int(datetime(2026, 11, 11, 9, 5, tzinfo=sh).timestamp())

    assert _format_delivered_label(today_ts, now_ts=now_ts) == "今天 12:18"
    assert _format_delivered_label(yesterday_ts, now_ts=now_ts) == "昨天 18:17"
    assert _format_delivered_label(older_ts, now_ts=now_ts) == "11月11日 09:05"
    assert _extract_total({"data": {"records": [{"id": 1}], "total": 42}}) == 42
    assert _extract_total({"data": [{"id": 1}], "page": {"pageNum": 1, "totalCount": 139}}) == 139
    assert _extract_total({"records": [{"id": 1}, {"id": 2}]}) == 2


def test_erp_role_mappers_numerical_contracts_and_status_semantics() -> None:
    from octop.infra.workbench.erp_source import (
        _CACHE_MAX_ENTRIES,
        _CACHE_LOCK,
        _ENDPOINT_CACHE,
        _map_ad_buy,
        _map_business,
        _map_customer_service,
        _map_finance,
        _map_mcn_ops,
        _map_media,
        _order_status,
        _prune_cache_locked,
    )

    # Cancelled order (9) must NOT count as done/100%
    assert _order_status(9) == ("cancelled", 0, "已取消")

    cs_slice = _map_customer_service(
        {
            "pending": {"waitComplete": 4, "waitPending": 1, "waitInvoiced": 2, "waitCollected": 1, "waitClaimed": 3},
            "orders": {
                "data": [
                    {"id": 1, "customerName": "甲", "status": 10},  # done
                    {"id": 2, "customerName": "乙", "status": 3},   # in_progress
                    {"id": 3, "customerName": "丙", "status": 9},   # cancelled (excluded!)
                ],
                "page": {"totalCount": 120},
            },
            "offers": {"data": [{"id": 10, "customerName": "甲", "status": 1}]},
            "sub_projects": {"data": [{"id": 20, "customerName": "甲", "hasOrder": True}]},
        },
        "7d",
    )
    assert cs_slice.kpis[0]["value"] == "120"
    assert cs_slice.kpis[3]["value"] == "50%"  # 1 done / 2 non-cancelled
    assert all("丙" != it["target_name"] for it in cs_slice.tasks)
    assert cs_slice.goal_progress["order_close"] == (1.0, 2.0)
    assert "quote_follow" not in cs_slice.goal_progress

    # Business offer status (1=已生成订单 -> done/100)
    biz_slice = _map_business(
        {
            "offers": {"data": [{"id": 1, "customerName": "甲", "status": 1}, {"id": 2, "customerName": "乙", "status": 0}]},
            "contracts": {"data": [{"id": 3, "customerName": "甲", "signingDate": "2026-11-10"}], "page": {"totalCount": 139}},
            "orders": {"data": [{"id": 4, "status": 10}, {"id": 5, "status": 9}], "page": {"totalCount": 80}},
            "perf": {"orderAmount": 1000, "receiveAmount": 500},
        },
        "7d",
    )
    offer_tasks = [t for t in biz_slice.tasks if t["external_ref"].startswith("mcn:offer:")]
    assert [t["status"] for t in offer_tasks] == ["done", "in_progress"]
    assert biz_slice.goal_progress["order_completion"] == (100.0, 100.0)
    assert biz_slice.goal_progress["contract_count"] == (139.0, 5.0)

    # ServiceItemDo.progress=1 is 1%, NOT 100%
    ops_slice = _map_mcn_ops(
        {
            "service_items": {
                "data": [
                    {"id": 11, "accountName": "达人A", "progress": 1},
                    {"id": 12, "accountName": "达人B", "progress": 100},
                ]
            }
        },
        "7d",
    )
    svc_tasks = {t["target_name"]: t for t in ops_slice.tasks}
    assert svc_tasks["达人A"]["progress"] == 1
    assert svc_tasks["达人A"]["status"] == "in_progress"
    assert svc_tasks["达人B"]["progress"] == 100
    assert svc_tasks["达人B"]["status"] == "done"
    assert ops_slice.goal_progress["svc_exec"] == (1.0, 2.0)

    # Ad-buy customerDistribution filters to parentId == 0 (retains single-level + drops child double count)
    ad_slice = _map_ad_buy(
        {
            "customer_dist": [
                {"advertisingCategoryId": 1, "parentId": 0, "advertisingCategoryName": "美妆一级", "settlementAmount": 600, "rate": 0.3},
                {"advertisingCategoryId": 2, "parentId": 1, "advertisingCategoryName": "美妆二级", "settlementAmount": 600, "rate": 1.0},
                {"advertisingCategoryId": 3, "parentId": "0", "advertisingCategoryName": "3C单层", "settlementAmount": 400, "rate": 0.2},
            ],
            "orders": {"data": [{"id": 1, "status": 10}, {"id": 2, "status": 9}]},
        },
        "7d",
    )
    assert ad_slice.kpis[0]["value"] == "2"
    assert "1,000" in ad_slice.kpis[0]["delta"]
    dist_tasks = [t for t in ad_slice.tasks if t["external_ref"].startswith("mcn:dist:")]
    assert [t["target_name"] for t in dist_tasks] == ["美妆一级", "3C单层"]
    assert all(t["status"] == "in_progress" and t["progress"] == 50 for t in dist_tasks)
    assert ad_slice.goal_progress["order_delivery"] == (1.0, 1.0)

    # Media accounts are active archives (not done/100), and service_items appear
    media_slice = _map_media(
        {
            "accounts": {"data": [{"id": 1, "accountName": "达人X", "accountNumber": "A1"}]},
            "service_items": {"data": [{"id": 2, "accountName": "达人Y", "progress": 60}]},
        },
        "7d",
    )
    by_name = {t["target_name"]: t for t in media_slice.tasks}
    assert by_name["达人X"]["status"] == "in_progress"
    assert by_name["达人X"]["progress"] < 100
    assert by_name["达人Y"]["progress"] == 60

    # Finance ReceiptRequestPageVo.status=1 -> done/100
    fin_slice = _map_finance(
        {"receipts": {"data": [{"id": 1, "customerName": "丁", "money": 500, "status": 1}]}},
        "7d",
    )
    assert fin_slice.tasks[0]["status"] == "done"
    assert fin_slice.tasks[0]["progress"] == 100

    # Cache bounding check
    with _CACHE_LOCK:
        _ENDPOINT_CACHE.clear()
        for i in range(_CACHE_MAX_ENTRIES + 10):
            _ENDPOINT_CACHE[(1, "fp", f"k{i}")] = (1000.0 + i, i)
        _prune_cache_locked(1010.0)
        assert len(_ENDPOINT_CACHE) < _CACHE_MAX_ENTRIES
        _ENDPOINT_CACHE.clear()


@pytest.mark.asyncio
async def test_workbench_meta_pagination_goal_preservation_and_permission_degradation(
    workbench_app, monkeypatch
) -> None:
    app, state, _ = workbench_app
    services = app.state.octop_server.services
    monkeypatch.setattr(
        app.state.octop_server,
        "services",
        dataclasses.replace(
            services,
            config=dataclasses.replace(
                services.config,
                youli=dataclasses.replace(services.config.youli, mcn_base_url="https://mcn.example"),
            ),
        ),
    )
    uid = state["user"].id
    invalidate_user_erp_cache(uid)
    now = int(time.time())
    services.youli_session_repo.create(
        sid="sid-meta-1",
        user_id=uid,
        account_user_id=201,
        account_username="15000000089",
        company_id=147372,
        employee_id=2823592,
        industry=None,
        token_blob=encrypt_secret(services.secret_repo, "mcn-token-meta"),
        token_expires_at=now + 3600,
        abs_expires_at=now + 7200,
    )

    today_str = time.strftime("%Y-%m-%d 09:00:00", time.localtime(now))
    old_str = time.strftime("%Y-%m-%d 09:00:00", time.localtime(now - 20 * 86400))

    def fake_post_meta(self, path, payload=None):
        if path == "/api/mcn/contract/list":
            return {
                "code": 200,
                "data": [
                    {
                        "id": 801,
                        "contractNumber": "HT-801",
                        "customerName": "茶颜",
                        "typeName": "年度框架",
                        "signingDate": today_str,
                        "createdTime": today_str,
                    }
                ],
                "page": {"pageNum": 1, "totalCount": 139},
            }
        if path == "/api/mcn/order/list":
            return {
                "code": 200,
                "data": [
                    {"id": 901, "customerName": "今日完成单", "status": 10, "signerTime": today_str},
                    {"id": 902, "customerName": "历史完成单", "status": 10, "signerTime": old_str},
                ],
                "page": {"pageNum": 1, "totalCount": 50},
            }
        return {"code": 200, "data": [], "page": {"pageNum": 1, "totalCount": 0}}

    def fake_get(self, path, params=None):
        if path.startswith("/api/ai-creator/"):
            raise McnPermissionError("仅数据管理员可操作此设置")
        return {}

    monkeypatch.setattr(McnClient, "post_with_meta", fake_post_meta)
    monkeypatch.setattr(McnClient, "post", lambda self, path, payload=None: {"orderAmount": 1000, "receiveAmount": 500})
    monkeypatch.setattr(McnClient, "get", fake_get)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # AI Creator 403 on deliverables must NOT mark MCN disconnected!
        delivs = (await client.get("/api/workbench/deliverables")).json()
        assert delivs["mcn_connected"] is True
        assert any(d["source"] == "mcn" for d in delivs["items"])
        assert "管理员" in delivs["mcn_error"]

        # Dashboard goal contract_count keeps default target 5 while current_value reflects page.totalCount=139
        dash = (await client.get("/api/workbench/dashboard", params={"role": "business", "range": "7d"})).json()
        contract_goal = next(g for g in dash["goals"] if g["title"] == "合同签署数量")
        assert contract_goal["current_value"] == 139.0
        assert contract_goal["target_value"] == 5.0
        assert contract_goal["percent"] == 100

        # Create deliverable + linked local done task -> must NOT double-count delivered_this_week
        await client.post(
            "/api/workbench/deliverables",
            json={
                "role_key": "customer_service",
                "deliverable_type": "document",
                "target_name": "茶颜",
                "title": "排期交付物",
                "content": "正文",
                "review_status": "approved",
                "source_thread_id": "th-9",
                "source_message_id": "msg-9",
            },
        )
        prog_today = (
            await client.get("/api/workbench/progress", params={"role": "customer_service", "range": "today"})
        ).json()
        prog_month = (
            await client.get("/api/workbench/progress", params={"role": "customer_service", "range": "month"})
        ).json()
        # In "today": 1 chat deliverable (its linked done task is excluded from double count) + 1 ERP done order from today = 2; historical order excluded
        assert prog_today["kpis"]["delivered_this_week"] == 2
        assert prog_month["kpis"]["delivered_this_week"] == 3
        today_names = {it["target_name"] for m in prog_today["modules"] for it in m["items"]}
        assert "今日完成单" in today_names
        assert "历史完成单" not in today_names
