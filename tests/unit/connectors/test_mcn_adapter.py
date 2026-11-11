from __future__ import annotations

from typing import Any

from octop.infra.connectors.gateway.adapters import mcn


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def post(self, path: str, payload: Any = None) -> Any:
        self.calls.append((path, payload))
        if path.endswith("/offerOrder/list"):
            return [{"id": "q1", "offerOrderNo": "BJ1"}]
        if path.endswith("/offer/order/detail"):
            return {"id": "q1"}
        return [{"id": "1", "accountName": "会火"}]

    def get(self, path: str, params: Any = None) -> Any:
        self.calls.append((path, params))
        return {"id": "q1", "factOrderMoney": 100}


def _use(monkeypatch, fake: FakeClient) -> None:
    monkeypatch.setattr(mcn, "_client", lambda creds: fake)
    mcn._SAVED_QUOTES.clear()


def test_tools_and_handlers_match() -> None:
    names = [tool["name"] for tool in mcn.list_tools()]
    assert len(names) == len(set(names))
    assert set(names) == set(mcn._HANDLERS)


def test_quote_cache_does_not_cross_employees() -> None:
    mcn._SAVED_QUOTES.clear()
    left = mcn.McnClient(None, "employee-a", base_url="http://mcn")
    right = mcn.McnClient(None, "employee-b", base_url="http://mcn")
    mcn._remember(left, "order:1", "a-result")
    assert mcn._recall(right, "order:1") is None
    assert mcn._recall(left, "order:1") == "a-result"


def test_package_quote_prices_quantity(monkeypatch) -> None:
    fake = FakeClient()
    fake.token_fingerprint = "employee-a"
    _use(monkeypatch, fake)
    mcn.call_tool(
        {},
        "youli_create_package_quote",
        {
            "name": "q",
            "product_id": "1",
            "customer_id": "2",
            "price_version_id": "3",
            "coupon_version_id": "4",
            "account_specs": [
                {"account_id": "10", "sku_id": "100", "price": "0.10", "quantity": 3},
                {"account_id": "11", "sku_id": "101", "price": "0.20", "quantity": 1},
            ],
            "confirm": True,
        },
    )
    payload = fake.calls[0][1]
    assert payload["orderMoney"] == 0.5
    assert payload["factOrderMoney"] == 0.5
    assert payload["itemRoList"][0]["quantity"] == 3
    assert payload["itemRoList"][0]["factMoney"] == 0.3
    assert payload["itemRoList"][1]["factMoney"] == 0.2


def test_foreign_turn_does_not_use_owner_token(monkeypatch) -> None:
    monkeypatch.setattr(mcn, "_turn_context", lambda: ("sid-other", 2))
    seen: dict[str, bool] = {}

    def latest() -> str:
        seen["latest"] = True
        return "owner-token"

    try:
        mcn._client(
            {
                "_owner_user_id": 1,
                "_token_resolver": lambda: "",
                "_latest_token_resolver": latest,
                "token": "stored-owner",
                "base_url": "http://mcn",
            }
        )
    except mcn.McnClientError:
        pass
    else:
        raise AssertionError("foreign turn borrowed the owner token")
    assert "latest" not in seen


def test_owner_background_uses_own_live_token(monkeypatch) -> None:
    monkeypatch.setattr(mcn, "_turn_context", lambda: ("", 1))
    client = mcn._client(
        {
            "_owner_user_id": 1,
            "_token_resolver": lambda: "",
            "_latest_token_resolver": lambda: "owner-live",
            "token": "stored-owner",
            "base_url": "http://mcn",
        }
    )
    expected = mcn.McnClient(None, "owner-live", base_url="http://mcn")
    assert client.token_fingerprint == expected.token_fingerprint


def test_matching_turn_uses_the_session_token(monkeypatch) -> None:
    monkeypatch.setattr(mcn, "_turn_context", lambda: ("sid-owner", 1))
    client = mcn._client(
        {
            "_owner_user_id": 1,
            "_token_resolver": lambda: "session-token",
            "_latest_token_resolver": lambda: "should-not-use",
            "token": "stored",
            "base_url": "http://mcn",
        }
    )
    expected = mcn.McnClient(None, "session-token", base_url="http://mcn")
    assert client.token_fingerprint == expected.token_fingerprint


def test_turn_resolver_uses_only_the_instance_owner(monkeypatch) -> None:
    from octop.infra.connectors.builder import _mcn_turn_token_resolver

    seen: dict[str, object] = {}

    class _Svc:
        def live_youli_token_for_sid(self, sid: str, owner_user_id: int | None = None) -> str:
            seen["sid"] = sid
            seen["owner"] = owner_user_id
            return "session-token"

    monkeypatch.setattr(
        "langgraph.config.get_config",
        lambda: {"configurable": {"youli_sid": "sid-1", "octop_user_id": 7}},
    )
    token = _mcn_turn_token_resolver(_Svc(), 7)()
    assert token == "session-token"
    assert seen == {"sid": "sid-1", "owner": 7}


def test_save_quote_cache_is_per_employee(monkeypatch) -> None:
    clients = {"a": FakeClient(), "b": FakeClient()}
    clients["a"].token_fingerprint = "employee-a"
    clients["b"].token_fingerprint = "employee-b"
    monkeypatch.setattr(mcn, "_client", lambda creds: clients[creds["who"]])
    mcn._SAVED_QUOTES.clear()
    payload = {"offerOrderName": "same"}
    args = {"payload": payload, "confirm": True}

    mcn.call_tool({"who": "a"}, "youli_save_quote", args)
    mcn.call_tool({"who": "b"}, "youli_save_quote", args)
    mcn.call_tool({"who": "a"}, "youli_save_quote", args)

    assert len(clients["a"].calls) == 1
    assert len(clients["b"].calls) == 1


def test_package_quote_rejects_bad_quantity(monkeypatch) -> None:
    fake = FakeClient()
    fake.token_fingerprint = "employee-a"
    _use(monkeypatch, fake)
    result = mcn.call_tool(
        {},
        "youli_create_package_quote",
        {
            "name": "q",
            "product_id": "1",
            "customer_id": "2",
            "price_version_id": "3",
            "coupon_version_id": "4",
            "account_specs": [{"account_id": "10", "sku_id": "100", "price": "1", "quantity": 0}],
            "confirm": True,
        },
    )
    assert "数量不正确" in result
    assert fake.calls == []


def test_missing_contract_is_reported(monkeypatch) -> None:
    class _EmptyContractClient(FakeClient):
        def get(self, path: str, params: Any = None) -> Any:
            self.calls.append((path, params))
            return None

    fake = _EmptyContractClient()
    _use(monkeypatch, fake)
    result = mcn.call_tool({}, "youli_get_contract", {"contract_id": "404"})
    assert "不存在" in result


def test_missing_token_is_reported(monkeypatch) -> None:
    monkeypatch.setattr(mcn, "McnClient", lambda *args, **kwargs: None)
    result = mcn.call_tool({}, "youli_list_accounts", {})
    assert "未授权" in result


def test_list_accounts_sends_filters(monkeypatch) -> None:
    fake = FakeClient()
    _use(monkeypatch, fake)
    mcn.call_tool(
        {"token": "t"},
        "youli_list_accounts",
        {"account_name": "会火", "platform": 1, "page": 2, "page_size": 50},
    )
    assert fake.calls[-1] == (
        "/api/mcn/account/list",
        {
            "page": {"pageNum": 2, "pageSize": 20},
            "accountName": "会火",
            "platforms": [1],
        },
    )


def test_write_tools_require_confirmation(monkeypatch) -> None:
    fake = FakeClient()
    _use(monkeypatch, fake)
    for name, args in (
        ("youli_create_project", {"project_name": "p"}),
        ("youli_create_contract", {"payload": {"enableType": 2}}),
        ("youli_update_publish_link", {"service_item_id": "1", "publish_link": "u"}),
    ):
        result = mcn.call_tool({"token": "t"}, name, args)
        assert "确认" in result or "未" in result
    assert fake.calls == []


def test_package_quote_builds_rows(monkeypatch) -> None:
    fake = FakeClient()
    _use(monkeypatch, fake)
    result = mcn.call_tool(
        {"token": "t"},
        "youli_create_package_quote",
        {
            "name": "拉包报价",
            "product_id": "p1",
            "customer_id": "c1",
            "price_version_id": "v1",
            "coupon_version_id": "v2",
            "account_specs": [
                {"account_id": "a1", "account_name": "会火大明星", "sku_id": "s1", "price": 7500, "supplier_id": "sup1"},
                {"account_id": "a2", "account_name": "会火", "sku_id": "s2", "price": 4000},
            ],
            "confirm": True,
        },
    )
    assert "offerOrderNo" in result
    assert fake.calls[0][0] == "/api/mcn/offerOrder/save"
    payload = fake.calls[0][1]
    assert payload["orderMoney"] == 11500
    assert [item["price"] for item in payload["itemRoList"]] == [7500.0, 4000.0]
    assert payload["itemRoList"][0]["supplierId"] == "sup1"
    assert '"supplier": "sup1"' in payload["itemRoList"][0]["dynamicItemObj"]


def test_order_items_get_fresh_ids(monkeypatch) -> None:
    fake = FakeClient()
    _use(monkeypatch, fake)

    def detail(path: str, params: Any = None) -> Any:
        fake.calls.append((path, params))
        return {
            "offerOrderNo": "BJ1",
            "customerId": "c1",
            "productId": "p1",
            "priceVersion": "v1",
            "couponVersion": "v2",
            "orderMoney": 100,
            "factOrderMoney": 100,
            "offerOrderItemVos": [
                {"id": "item-1", "factMoney": 100, "originalPrice": 100, "quantity": 1}
            ],
        }

    monkeypatch.setattr(fake, "get", detail)
    mcn.call_tool({"token": "t"}, "youli_create_order_from_quote", {"quote_id": "q1", "confirm": True})
    save = [call for call in fake.calls if call[0] == "/api/mcn/order/temporaryStorage"][0]
    assert save[1]["offerOrderSaveRo"]["itemRoList"][0]["itemId"] is None


def test_company_entities_use_public_list(monkeypatch) -> None:
    fake = FakeClient()
    _use(monkeypatch, fake)
    mcn.call_tool({"token": "t"}, "youli_list_company_entities", {})
    assert fake.calls[-1][0] == "/api/amoeba/companyEntity/list"


def test_opportunities_use_customer_opportunity_list(monkeypatch) -> None:
    fake = FakeClient()
    _use(monkeypatch, fake)
    mcn.call_tool({"token": "t"}, "youli_list_opportunities", {"keyword": "超级飞侠"})
    path, payload = fake.calls[-1]
    assert path == "/api/mcn/customer/opportunity/list"
    assert payload["keyword"] == "超级飞侠"


def test_dictionaries_and_projects(monkeypatch) -> None:
    fake = FakeClient()
    _use(monkeypatch, fake)
    mcn.call_tool({"token": "t"}, "youli_list_dictionaries", {"enum_keys": ["project_type"]})
    assert fake.calls[-1][0] == "/api/mcn/enum/data/dictionary/getEnumList"
    assert fake.calls[-1][1] == {"enumKeys": ["project_type"]}
    mcn.call_tool({"token": "t"}, "youli_list_projects", {"customer_opportunity_id": "o1"})
    assert fake.calls[-1][0] == "/api/mcn/customerSubProject/list"
    assert fake.calls[-1][1]["customerId"] == "o1"


def test_create_sub_project_payload(monkeypatch) -> None:
    fake = FakeClient()
    _use(monkeypatch, fake)
    mcn.call_tool(
        {"token": "t"},
        "youli_create_sub_project",
        {
            "project_name": "AI测试项目-冲榜",
            "customer_opportunity_id": "o1",
            "customer_name": "超级飞侠",
            "project_type": 2,
            "main_platform": 1,
            "project_budget": 100000,
            "project_leader_ids": ["2823591"],
            "confirm": True,
        },
    )
    path, payload = fake.calls[-1]
    assert path == "/api/mcn/customerSubProject/add"
    assert payload["projectType"] == 2
    assert payload["mainPlatform"] == 1
    assert payload["customerName"] == "超级飞侠"


def test_publish_link_truncates_time(monkeypatch) -> None:
    fake = FakeClient()
    _use(monkeypatch, fake)
    mcn.call_tool(
        {"token": "t"},
        "youli_update_publish_link",
        {
            "service_item_id": "s1",
            "publish_link": "https://www.bilibili.com/video/BV1xx",
            "publish_time": "2026-11-10 12:00:00",
            "confirm": True,
        },
    )
    payload = fake.calls[-1][1]
    assert payload["publishTime"] == "2026-11-10"


def test_create_contract_fills_issuing_office_and_period(monkeypatch) -> None:
    fake = FakeClient()
    _use(monkeypatch, fake)
    mcn.call_tool(
        {"token": "t"},
        "youli_create_contract",
        {
            "name": "AI测试合同",
            "customer_id": "c1",
            "customer_opportunity_id": "o1",
            "type": 1,
            "issuing_office": "147372",
            "effective_time": "2026-11-10",
            "expiration_time": "2027-11-10",
            "price_version_id": "v1",
            "coupon_version_id": "v2",
            "confirm": True,
        },
    )
    path, payload = fake.calls[-1]
    assert path == "/api/mcn/contract/addContract"
    assert payload["issuingOffice"] == "147372"
    assert payload["contractAccountInfo"] == {"periodType": 1}
    assert payload["protectionPrice"] == 1
    assert payload["priceVersionBindList"] == [{"versionId": "v1"}]


def test_create_contract_can_use_opportunity(monkeypatch) -> None:
    fake = FakeClient()
    _use(monkeypatch, fake)
    mcn.call_tool(
        {"token": "t"},
        "youli_create_contract",
        {
            "name": "AI测试合同-商机",
            "customer_opportunity_id": "2043940878993854465",
            "type": 2,
            "issuing_office": "147372",
            "effective_time": "2026-11-10",
            "expiration_time": "2027-11-10",
            "confirm": True,
        },
    )
    payload = fake.calls[-1][1]
    assert payload["customerOpportunityId"] == "2043940878993854465"
    assert "customerId" not in payload


def test_create_contract_requires_opportunity(monkeypatch) -> None:
    fake = FakeClient()
    _use(monkeypatch, fake)
    result = mcn.call_tool(
        {"token": "t"},
        "youli_create_contract",
        {
            "name": "无商机合同",
            "customer_id": "c1",
            "type": 1,
            "issuing_office": "147372",
            "effective_time": "2026-11-10",
            "expiration_time": "2027-11-10",
            "confirm": True,
        },
    )
    assert "customer_opportunity_id" in result
    assert fake.calls == []
