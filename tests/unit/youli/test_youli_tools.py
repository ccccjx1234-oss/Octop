"""Tool-level tests: delegated sid lookup and request shaping."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from octop.config import YouliConfig
from octop.infra.auth.youli import tools as youli_tools
from octop.infra.auth.youli.context import set_current_youli_sid
from octop.infra.auth.youli.mcn_client import McnClient


class _SessionRepo:
    def __init__(self, row: object | None) -> None:
        self._row = row

    def get_active(self, sid: str):
        return self._row if sid == "sid-1" else None


def _tools(row: object | None):
    repos = SimpleNamespace(
        youli_session_repo=_SessionRepo(row), secret_repo=object()
    )
    return {
        tool.name: tool
        for tool in youli_tools.build_youli_tools(
            repos=repos, cfg=YouliConfig(enabled=True, mcn_base_url="http://mcn")
        )
    }


@pytest.fixture(autouse=True)
def _reset_sid():
    set_current_youli_sid(None)
    youli_tools._SAVED_QUOTES.clear()
    yield
    set_current_youli_sid(None)
    youli_tools._SAVED_QUOTES.clear()


@pytest.mark.asyncio
async def test_account_tool_uses_delegated_session(monkeypatch) -> None:
    set_current_youli_sid("sid-1")
    monkeypatch.setattr(youli_tools, "decrypt_secret", lambda repo, blob: "tok")
    calls: list = []

    def fake_post(self, path, payload=None):
        calls.append((path, payload))
        return [{"accountId": "1", "accountName": "测试账号"}]

    monkeypatch.setattr(McnClient, "post", fake_post)

    result = await _tools(SimpleNamespace(token_blob=b"blob"))[
        "youli_list_accounts"
    ].ainvoke({"account_name": "测试", "platform": 1})

    assert "测试账号" in result
    assert calls == [
        (
            "/api/mcn/account/list",
            {
                "page": {"pageNum": 1, "pageSize": 20},
                "accountName": "测试",
                "platforms": [1],
            },
        )
    ]


@pytest.mark.asyncio
async def test_tools_refuse_without_sid() -> None:
    result = await _tools(SimpleNamespace(token_blob=b"blob"))[
        "youli_list_quotes"
    ].ainvoke({})

    assert result.startswith("查询报价单失败")
    assert "没有经贝员工授权" in result


@pytest.mark.asyncio
async def test_tools_refuse_revoked_session(monkeypatch) -> None:
    set_current_youli_sid("sid-1")
    result = await _tools(None)["youli_get_quote"].ainvoke({"quote_id": "9"})

    assert "登录已失效" in result


@pytest.mark.asyncio
async def test_quote_list_rejects_oversized_page() -> None:
    set_current_youli_sid("sid-1")

    with pytest.raises(Exception, match="less than or equal to 20"):
        await _tools(SimpleNamespace(token_blob=b"blob"))[
            "youli_list_quotes"
        ].ainvoke({"keyword": "拉包", "page": 2, "page_size": 50})


@pytest.mark.asyncio
async def test_quote_list_passes_filters(monkeypatch) -> None:
    set_current_youli_sid("sid-1")
    monkeypatch.setattr(youli_tools, "decrypt_secret", lambda repo, blob: "tok")
    calls: list = []

    def fake_post(self, path, payload=None):
        calls.append(payload)
        return []

    monkeypatch.setattr(McnClient, "post", fake_post)

    await _tools(SimpleNamespace(token_blob=b"blob"))["youli_list_quotes"].ainvoke(
        {"keyword": "拉包", "page": 2}
    )

    assert calls[0] == {
        "page": {"pageNum": 2, "pageSize": 10},
        "offerOrderName": "拉包",
    }


@pytest.mark.asyncio
async def test_save_quote_requires_confirmation(monkeypatch) -> None:
    set_current_youli_sid("sid-1")
    monkeypatch.setattr(youli_tools, "decrypt_secret", lambda repo, blob: "tok")
    calls: list = []

    def fake_post(self, path, payload=None):
        calls.append((path, payload))
        return True

    monkeypatch.setattr(McnClient, "post", fake_post)

    result = await _tools(SimpleNamespace(token_blob=b"blob"))[
        "youli_save_quote"
    ].ainvoke({"payload": {"productId": "1"}})

    assert result.startswith("未保存")
    assert calls == []


@pytest.mark.asyncio
async def test_save_quote_is_idempotent(monkeypatch) -> None:
    set_current_youli_sid("sid-1")
    monkeypatch.setattr(youli_tools, "decrypt_secret", lambda repo, blob: "tok")
    calls: list = []

    def fake_post(self, path, payload=None):
        calls.append((path, payload))
        return True

    monkeypatch.setattr(McnClient, "post", fake_post)
    tool = _tools(SimpleNamespace(token_blob=b"blob"))["youli_save_quote"]

    first = await tool.ainvoke({"payload": {"productId": "1"}, "confirm": True})
    second = await tool.ainvoke({"payload": {"productId": "1"}, "confirm": True})

    assert first == "true"
    assert second.startswith("重复提交已忽略")
    assert calls == [("/api/mcn/offerOrder/save", {"productId": "1"})]


@pytest.mark.asyncio
async def test_save_quote_idempotency_does_not_cross_employees(monkeypatch) -> None:
    set_current_youli_sid("sid-1")
    current = {"token": "employee-a"}
    monkeypatch.setattr(youli_tools, "decrypt_secret", lambda repo, blob: current["token"])
    calls: list = []

    def fake_post(self, path, payload=None):
        calls.append((self._token, path, payload))
        return True

    monkeypatch.setattr(McnClient, "post", fake_post)
    tool = _tools(SimpleNamespace(token_blob=b"blob"))["youli_save_quote"]
    payload = {"productId": "1"}

    await tool.ainvoke({"payload": payload, "confirm": True})
    current["token"] = "employee-b"
    await tool.ainvoke({"payload": payload, "confirm": True})

    assert [item[0] for item in calls] == ["employee-a", "employee-b"]


@pytest.mark.asyncio
async def test_create_package_quote_builds_rows(monkeypatch) -> None:
    set_current_youli_sid("sid-1")
    monkeypatch.setattr(youli_tools, "decrypt_secret", lambda repo, blob: "tok")
    calls: list = []

    def fake_post(self, path, payload=None):
        calls.append((path, payload))
        if path.endswith("/list"):
            return [{"id": "99", "offerOrderNo": "BJ1", "offerOrderName": "拉包测试"}]
        return True

    monkeypatch.setattr(McnClient, "post", fake_post)

    result = await _tools(SimpleNamespace(token_blob=b"blob"))[
        "youli_create_package_quote"
    ].ainvoke(
        {
            "name": "拉包测试",
            "product_id": "1",
            "customer_id": "2",
            "price_version_id": "3",
            "coupon_version_id": "4",
            "account_specs": [
                {"account_id": "10", "account_name": "A", "sku_id": "100", "price": 7500},
                {"account_id": "11", "account_name": "B", "sku_id": "101", "price": 4000},
            ],
            "confirm": True,
        }
    )

    assert "BJ1" in result
    path, payload = calls[0]
    assert path == "/api/mcn/offerOrder/save"
    assert payload["orderMoney"] == 11500
    assert payload["factOrderMoney"] == 11500
    assert payload["priceVersion"] == "3"
    assert payload["itemRoList"][0] == {
        "type": 3,
        "modeOperation": 1,
        "skuId": "100",
        "accountId": "10",
        "accountName": "A",
        "quantity": 1,
        "price": 7500.0,
        "originalPrice": 7500.0,
        "originalMoney": 7500.0,
        "couponAfterMoney": 7500.0,
        "factMoney": 7500.0,
        "noCalculate": 0,
    }


@pytest.mark.asyncio
async def test_create_package_quote_multiplies_quantity(monkeypatch) -> None:
    set_current_youli_sid("sid-1")
    monkeypatch.setattr(youli_tools, "decrypt_secret", lambda repo, blob: "tok")
    calls: list = []

    def fake_post(self, path, payload=None):
        calls.append((path, payload))
        return True

    monkeypatch.setattr(McnClient, "post", fake_post)
    result = await _tools(SimpleNamespace(token_blob=b"blob"))[
        "youli_create_package_quote"
    ].ainvoke(
        {
            "name": "小数",
            "product_id": "1",
            "customer_id": "2",
            "price_version_id": "3",
            "coupon_version_id": "4",
            "account_specs": [
                {
                    "account_id": "10",
                    "account_name": "A",
                    "sku_id": "100",
                    "price": "0.10",
                    "quantity": 3,
                },
                {
                    "account_id": "11",
                    "account_name": "B",
                    "sku_id": "101",
                    "price": "0.20",
                    "quantity": 1,
                },
            ],
            "confirm": True,
        }
    )

    assert "true" in result
    payload = calls[0][1]
    assert payload["orderMoney"] == 0.5
    assert payload["factOrderMoney"] == 0.5
    assert payload["itemRoList"][0]["quantity"] == 3
    assert payload["itemRoList"][0]["factMoney"] == 0.3
    assert payload["itemRoList"][1]["originalMoney"] == 0.2


@pytest.mark.asyncio
async def test_create_order_from_quote_maps_snapshot(monkeypatch) -> None:
    set_current_youli_sid("sid-1")
    monkeypatch.setattr(youli_tools, "decrypt_secret", lambda repo, blob: "tok")
    calls: list = []

    detail = {
        "customerId": "2",
        "productId": "1",
        "priceVersion": "3",
        "couponVersion": "4",
        "orderMoney": 7500.0,
        "factOrderMoney": 7500.0,
        "orderType": 1,
        "offerOrderNo": "BJ1",
        "offerOrderItemVos": [
            {
                "itemId": "9",
                "type": 3,
                "skuId": "100",
                "accountId": "10",
                "accountName": "A",
                "quantity": 1,
                "originalPrice": 7500,
                "originalMoney": 7500,
                "baseMoney": 7500,
                "factMoney": 7500,
                "modeOperation": 1,
                "noCalculate": 0,
            }
        ],
    }

    def fake_get(self, path, params=None):
        calls.append((path, params))
        return detail

    def fake_post(self, path, payload=None):
        calls.append((path, payload))
        return {"orderId": "77", "result": True}

    monkeypatch.setattr(McnClient, "get", fake_get)
    monkeypatch.setattr(McnClient, "post", fake_post)

    result = await _tools(SimpleNamespace(token_blob=b"blob"))[
        "youli_create_order_from_quote"
    ].ainvoke({"quote_id": "5", "confirm": True})

    assert "77" in result
    assert calls[0] == ("/api/mcn/offerOrder/detail", {"id": "5"})
    path, payload = calls[1]
    assert path == "/api/mcn/order/temporaryStorage"
    assert payload["submitMethod"] == 2
    assert payload["orderRo"]["customerId"] == "2"
    assert payload["orderRo"]["offerOrderId"] == "5"
    assert payload["offerOrderSaveRo"]["itemRoList"][0]["couponAfterMoney"] == 7500


@pytest.mark.asyncio
async def test_create_combined_order_groups_by_supplier(monkeypatch) -> None:
    set_current_youli_sid("sid-1")
    monkeypatch.setattr(youli_tools, "decrypt_secret", lambda repo, blob: "tok")
    posts: list = []

    def item(account: str, supplier: str, amount: float) -> dict:
        return {
            "itemId": account,
            "type": 3,
            "skuId": "100",
            "accountId": account,
            "quantity": 1,
            "originalPrice": amount,
            "originalMoney": amount,
            "baseMoney": amount,
            "factMoney": amount,
            "modeOperation": 1,
            "dynamicItemObj": f'{{"formLabelAlign":{{"supplier":"{supplier}"}}}}',
        }

    detail = {
        "customerId": "2",
        "productId": "1",
        "priceVersion": "3",
        "couponVersion": "4",
        "orderType": 1,
        "offerOrderNo": "BJ9",
        "offerOrderItemVos": [item("10", "501", 7500), item("11", "502", 4000)],
    }

    monkeypatch.setattr(McnClient, "get", lambda self, path, params=None: detail)

    def fake_post(self, path, payload=None):
        posts.append(payload)
        return {"orderId": str(len(posts)), "result": True}

    monkeypatch.setattr(McnClient, "post", fake_post)

    result = await _tools(SimpleNamespace(token_blob=b"blob"))[
        "youli_create_combined_order"
    ].ainvoke({"quote_id": "9", "confirm": True})

    assert "\"groups\": 2" in result
    assert len(posts) == 2
    amounts = sorted(post["offerOrderSaveRo"]["factOrderMoney"] for post in posts)
    assert amounts == [4000.0, 7500.0]
    assert all(len(post["offerOrderSaveRo"]["itemRoList"]) == 1 for post in posts)
    assert "offerOrderId" not in posts[0]


@pytest.mark.asyncio
async def test_update_publish_link_requires_confirmation(monkeypatch) -> None:
    set_current_youli_sid("sid-1")
    monkeypatch.setattr(youli_tools, "decrypt_secret", lambda repo, blob: "tok")
    calls: list = []

    def fake_post(self, path, payload=None):
        calls.append((path, payload))
        return True

    monkeypatch.setattr(McnClient, "post", fake_post)
    tool = _tools(SimpleNamespace(token_blob=b"blob"))["youli_update_publish_link"]

    blocked = await tool.ainvoke({"service_item_id": "7", "publish_link": "https://a"})
    assert blocked.startswith("未保存")
    assert calls == []

    await tool.ainvoke(
        {
            "service_item_id": "7",
            "publish_link": " https://a ",
            "publish_time": "2026-11-10 10:00:00",
            "confirm": True,
        }
    )
    assert calls == [
        (
            "/api/mcn/order/service/item/edit",
            {
                "id": "7",
                "publishLink": "https://a",
                "publishTime": "2026-11-10 10:00:00",
            },
        )
    ]


@pytest.mark.asyncio
async def test_export_delivery_selects_path(monkeypatch) -> None:
    set_current_youli_sid("sid-1")
    monkeypatch.setattr(youli_tools, "decrypt_secret", lambda repo, blob: "tok")
    calls: list = []

    def fake_post(self, path, payload=None):
        calls.append((path, payload))
        return True

    monkeypatch.setattr(McnClient, "post", fake_post)
    tool = _tools(SimpleNamespace(token_blob=b"blob"))["youli_export_delivery"]

    monkeypatch.setattr(
        "octop.infra.auth.youli.delivery_export.time.sleep", lambda _seconds: None
    )
    blocked = await tool.ainvoke({"mode": "project", "project_name": "测试项目"})
    assert blocked.startswith("未导出")
    assert calls == []

    await tool.ainvoke(
        {
            "mode": "project",
            "project_name": "测试项目",
            "export_template_id": "12",
            "confirm": True,
        }
    )
    export_calls = [call for call in calls if "deliveryDashboard" in call[0]]
    assert export_calls == [
        (
            "/api/mcn/deliveryDashboard/project/export",
            {
                "page": {"pageNum": 1, "pageSize": 20},
                "projectName": "测试项目",
            },
        )
    ]

    await tool.ainvoke({"mode": "link", "export_template_id": "12", "confirm": True})
    link_calls = [call for call in calls if call[0].endswith("/link/export")]
    assert link_calls[-1] == (
        "/api/mcn/deliveryDashboard/link/export",
        {"page": {"pageNum": 1, "pageSize": 20}, "exportTemplateId": "12"},
    )


@pytest.mark.asyncio
async def test_delivery_lookup_tools_call_helpers_not_themselves(monkeypatch) -> None:
    set_current_youli_sid("sid-1")
    monkeypatch.setattr(youli_tools, "decrypt_secret", lambda repo, blob: "tok")
    calls = []

    def fake_post(self, path, payload=None):
        calls.append((path, payload))
        return []

    monkeypatch.setattr(McnClient, "post", fake_post)
    tools = _tools(SimpleNamespace(token_blob=b"blob"))
    await tools["youli_list_delivery_projects"].ainvoke({"keyword": "杜甫江阁"})
    await tools["youli_list_export_templates"].ainvoke({"template_name": "周报"})
    assert [path for path, _ in calls] == [
        "/api/mcn/deliveryDashboard/project/list",
        "/api/mcn/excel/export/template/list",
    ]


@pytest.mark.asyncio
async def test_create_project_gate(monkeypatch) -> None:
    set_current_youli_sid("sid-1")
    monkeypatch.setattr(youli_tools, "decrypt_secret", lambda repo, blob: "tok")
    calls: list = []

    def fake_post(self, path, payload=None):
        calls.append((path, payload))
        return True

    monkeypatch.setattr(McnClient, "post", fake_post)
    tool = _tools(SimpleNamespace(token_blob=b"blob"))["youli_create_project"]

    blocked = await tool.ainvoke({"project_name": "AI测试项目"})
    assert blocked.startswith("未创建")
    assert calls == []

    await tool.ainvoke({"project_name": " AI测试项目 ", "confirm": True})
    assert calls == [
        ("/api/mcn/customerProject/addProject", {"projectName": "AI测试项目"})
    ]


@pytest.mark.asyncio
async def test_create_coupon_version_payload(monkeypatch) -> None:
    set_current_youli_sid("sid-1")
    monkeypatch.setattr(youli_tools, "decrypt_secret", lambda repo, blob: "tok")
    calls: list = []

    def fake_post(self, path, payload=None):
        calls.append((path, payload))
        return "id-1"

    monkeypatch.setattr(McnClient, "post", fake_post)

    await _tools(SimpleNamespace(token_blob=b"blob"))[
        "youli_create_coupon_version"
    ].ainvoke(
        {
            "name": "客户A专属优惠",
            "opportunity_id": "88",
            "is_default": True,
            "confirm": True,
        }
    )

    assert calls[0] == (
        "/api/mcn/coupon/version/addCouponVersion",
        {"name": "客户A专属优惠", "isDefault": 1, "opportunityId": "88"},
    )


@pytest.mark.asyncio
async def test_create_contract_requires_enable_type(monkeypatch) -> None:
    set_current_youli_sid("sid-1")
    monkeypatch.setattr(youli_tools, "decrypt_secret", lambda repo, blob: "tok")
    calls: list = []
    monkeypatch.setattr(
        McnClient,
        "post",
        lambda self, path, payload=None: calls.append((path, payload)) or True,
    )
    tool = _tools(SimpleNamespace(token_blob=b"blob"))["youli_create_contract"]

    assert (await tool.ainvoke({"payload": {}, "confirm": True})).startswith("未创建")
    assert calls == []
    await tool.ainvoke({"payload": {"enableType": 1, "name": "合同A"}, "confirm": True})
    assert calls[0][0] == "/api/mcn/contract/addContract"
