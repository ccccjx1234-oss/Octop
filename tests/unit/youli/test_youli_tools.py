"""Tool-level tests: delegated sid lookup and request shaping."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from octop.config import YouliConfig
from octop.infra.auth.youli import tools as youli_tools
from octop.infra.auth.youli.context import set_current_youli_sid
from octop.infra.auth.youli.mcn_client import McnClient
from octop.infra.connectors.gateway.adapters import mcn as mcn_adapter


class _SessionRepo:
    def __init__(self, row: object | None) -> None:
        self._row = row

    def get_active(self, sid: str):
        return self._row if sid == "sid-1" else None


def _tools(row: object | None):
    repos = SimpleNamespace(youli_session_repo=_SessionRepo(row), secret_repo=object())
    return {
        tool.name: tool
        for tool in youli_tools.build_youli_tools(
            repos=repos, cfg=YouliConfig(enabled=True, mcn_base_url="http://mcn")
        )
    }


@pytest.fixture(autouse=True)
def _reset_sid():
    set_current_youli_sid(None)
    mcn_adapter._SAVED_QUOTES.clear()
    yield
    set_current_youli_sid(None)
    mcn_adapter._SAVED_QUOTES.clear()


@pytest.mark.asyncio
async def test_account_tool_uses_delegated_session(monkeypatch) -> None:
    set_current_youli_sid("sid-1")
    monkeypatch.setattr(youli_tools, "decrypt_secret", lambda repo, blob: "tok")
    calls: list = []

    def fake_post(self, path, payload=None):
        calls.append((path, payload))
        return [{"accountId": "1", "accountName": "测试账号"}]

    monkeypatch.setattr(McnClient, "post", fake_post)

    result = await _tools(SimpleNamespace(token_blob=b"blob"))["youli_list_accounts"].ainvoke(
        {"account_name": "测试", "platform": 1}
    )

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
    result = await _tools(SimpleNamespace(token_blob=b"blob"))["youli_list_quotes"].ainvoke({})

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
        await _tools(SimpleNamespace(token_blob=b"blob"))["youli_list_quotes"].ainvoke(
            {"keyword": "拉包", "page": 2, "page_size": 50}
        )


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

    result = await _tools(SimpleNamespace(token_blob=b"blob"))["youli_save_quote"].ainvoke(
        {"payload": {"productId": "1"}}
    )

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
async def test_write_tools_delegate_to_connector_adapter(monkeypatch) -> None:
    """报价与下单只有一份实现，旧入口只转发参数，不再自行拼载荷。"""
    from octop.infra.connectors.gateway.adapters import mcn as adapter

    set_current_youli_sid("sid-1")
    monkeypatch.setattr(youli_tools, "decrypt_secret", lambda repo, blob: "tok")
    seen: list = []

    def delegate(name: str):
        def fake(client, args):
            seen.append((name, client, args))
            return f"{name}-ok"

        return fake

    for name in (
        "create_package_quote",
        "create_order_from_quote",
        "create_combined_order",
    ):
        monkeypatch.setattr(adapter, name, delegate(name))

    tools = _tools(SimpleNamespace(token_blob=b"blob"))
    assert (
        await tools["youli_create_package_quote"].ainvoke(
            {
                "name": "q",
                "product_id": "p",
                "customer_id": "c",
                "account_specs": [{"account_id": "a", "sku_id": "s"}],
                "confirm": True,
            }
        )
        == "create_package_quote-ok"
    )
    assert (
        await tools["youli_create_order_from_quote"].ainvoke({"quote_id": "5", "confirm": True})
        == "create_order_from_quote-ok"
    )
    assert (
        await tools["youli_create_combined_order"].ainvoke({"quote_id": "9", "confirm": True})
        == "create_combined_order-ok"
    )

    package = seen[0][2]
    assert package["account_specs"] == [{"account_id": "a", "sku_id": "s"}]
    assert package["price_version_id"] == ""
    assert package["coupon_version_id"] == ""
    assert package["project_type"] is None
    assert package["order_type"] == 1
    assert package["confirm"] is True
    assert seen[1][2] == {
        "quote_id": "5",
        "project_id": "",
        "sub_project_id": "",
        "confirm": True,
    }
    assert seen[2][2] == {"quote_id": "9", "confirm": True}
    assert all(isinstance(entry[1], McnClient) for entry in seen)


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

    monkeypatch.setattr("octop.infra.auth.youli.delivery_export.time.sleep", lambda _seconds: None)
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
    assert calls == [("/api/mcn/customerProject/addProject", {"projectName": "AI测试项目"})]


@pytest.mark.asyncio
async def test_create_coupon_version_payload(monkeypatch) -> None:
    set_current_youli_sid("sid-1")
    monkeypatch.setattr(youli_tools, "decrypt_secret", lambda repo, blob: "tok")
    calls: list = []

    def fake_post(self, path, payload=None):
        calls.append((path, payload))
        return "id-1"

    monkeypatch.setattr(McnClient, "post", fake_post)

    await _tools(SimpleNamespace(token_blob=b"blob"))["youli_create_coupon_version"].ainvoke(
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
