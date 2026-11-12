from __future__ import annotations

import json
from typing import Any

from octop.infra.connectors.gateway.adapters import mcn


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def post(self, path: str, payload: Any = None) -> Any:
        self.calls.append((path, payload))
        if path.endswith("/product/list"):
            return [{"id": "p1", "productType": 1, "attribute": -1},
                    {"id": "1", "productType": 1, "attribute": -1}]
        if path.endswith("/getEnumList"):
            return [{"enumKey": "project_type", "enumDataDictionaryVos": [
                {"enumValue": 6, "description": "散投"}]}]
        if path.endswith("/queryByProductSpecList"):
            pairs = [("a1", "s1"), ("a2", "s2"), ("10", "100"), ("11", "101")]
            return [{"accountId": a, "id": s, "accountName": "会火" + a,
                     "specName": "娱乐/视频<60s", "ad_category": "4", "publish_category": "10",
                     "modeOperation": 2 if a == "a1" else 1,
                     "supplierId": "sup1" if a == "a1" else None,
                     "platform": 1, "linkUrl": "/douyin.png", "price": 7500}
                    for a, s in pairs]
        if path.endswith("/calculateCoupon"):
            return {"orderItems": [{**i, "couponDetailVos": []} for i in payload["orderItems"]]}
        if path.endswith("/offerOrder/save"):
            return "q1"
        if path.endswith("/offerOrder/list"):
            return [{"id": "q1", "offerOrderNo": "BJ1"}]
        if path.endswith("/offer/order/detail"):
            return {"id": "q1"}
        return [{"id": "1", "accountName": "会火"}]

    def get(self, path: str, params: Any = None) -> Any:
        self.calls.append((path, params))
        return {"id": "q1", "offerOrderNo": "BJ1", "factOrderMoney": 100}


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
    payload = next(p for path, p in fake.calls if path.endswith("/offerOrder/save"))
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
    payload = next(p for path, p in fake.calls if path.endswith("/offerOrder/save"))
    assert payload["orderMoney"] == 11500
    assert [item["price"] for item in payload["itemRoList"]] == [7500.0, 4000.0]
    assert payload["itemRoList"][0]["supplierId"] == "sup1"
    assert '"supplier": "sup1"' in payload["itemRoList"][0]["dynamicItemObj"]
    assert payload["projectType"] == 6
    assert payload["orderType"] == 1
    assert [item["type"] for item in payload["itemRoList"]] == [0, 0]
    assert payload["itemRoList"][0]["modeOperation"] == 2
    for item in payload["itemRoList"]:
        display = json.loads(item["dynamicItemObj"])
        assert display["accountName"]
        assert display["formLabelAlign_name"] == "娱乐/视频<60s"
        assert display["formLabelAlign"]["ad_category"] == "4"
        assert display["spanMethod"] == []


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
            "projectType": 6,
            "customerOpportunityId": "opp1",
            "contractId": "contract1",
            "signerId": "employee1",
            "offerOrderItemVos": [
                {"id": "item-1", "factMoney": 100, "originalPrice": 100, "quantity": 1}
            ],
        }

    monkeypatch.setattr(fake, "get", detail)
    mcn.call_tool(
        {"token": "t"}, "youli_create_order_from_quote", {"quote_id": "q1", "confirm": True}
    )
    save = [call for call in fake.calls if call[0] == "/api/mcn/order/temporaryStorage"][0]
    assert save[1]["offerOrderSaveRo"]["itemRoList"][0]["itemId"] is None
    assert save[1]["offerOrderSaveRo"]["projectType"] == 6
    assert save[1]["offerOrderSaveRo"]["customerOpportunityId"] == "opp1"
    assert save[1]["offerOrderSaveRo"]["contractId"] == "contract1"
    assert save[1]["offerOrderSaveRo"]["signerId"] == "employee1"


def _quote_args(**updates: Any) -> dict[str, Any]:
    return {
        "name": "q",
        "product_id": "p1",
        "customer_id": "c1",
        "price_version_id": "v1",
        "coupon_version_id": "v2",
        "account_specs": [{"account_id": "a2", "sku_id": "s2", "price": 100}],
        "confirm": True,
        **updates,
    }


def test_package_quote_preview_uses_page_defaults_and_calculated_price(monkeypatch) -> None:
    class DefaultsClient(FakeClient):
        def post(self, path: str, payload: Any = None) -> Any:
            if path.endswith("/version/list"):
                self.calls.append((path, payload))
                if payload["page"]["pageNum"] == 1:
                    return [{"id": str(i), "isDefault": 0} for i in range(100)]
                return [{"id": "default-version", "isDefault": 1}]
            result = super().post(path, payload)
            if path.endswith("/calculateCoupon"):
                result["orderItems"][0]["couponAfterMoney"] = 6000
                result["orderItems"][0]["couponDetailVos"] = [{"name": "test discount"}]
            return result

    fake = DefaultsClient()
    _use(monkeypatch, fake)
    args = _quote_args(
        confirm=False,
        price_version_id="",
        coupon_version_id="",
        account_specs=[{"account_id": "a2", "sku_id": "s2"}],
    )
    preview = json.loads(mcn.call_tool({}, "youli_create_package_quote", args))
    assert preview["saved"] is False
    assert preview["draft"]["priceVersion"] == "default-version"
    assert preview["draft"]["couponVersion"] == "default-version"
    assert preview["draft"]["factOrderMoney"] == 6000
    assert preview["items"][0]["price"] == 7500
    assert not any(path.endswith("/save") for path, _ in fake.calls)


def test_package_quote_missing_spec_cannot_save(monkeypatch) -> None:
    fake = FakeClient()
    _use(monkeypatch, fake)
    result = mcn.call_tool(
        {},
        "youli_create_package_quote",
        _quote_args(account_specs=[{"account_id": "a2", "sku_id": "unknown", "price": 100}]),
    )
    assert "未保存" in result
    assert not any(path.endswith("/save") for path, _ in fake.calls)


def test_package_quote_remembers_save_even_when_readback_fails(monkeypatch) -> None:
    class ReadbackFailure(FakeClient):
        def get(self, path: str, params: Any = None) -> Any:
            raise mcn.McnClientError("timeout")

    fake = ReadbackFailure()
    _use(monkeypatch, fake)
    first = mcn.call_tool({}, "youli_create_package_quote", _quote_args())
    second = mcn.call_tool({}, "youli_create_package_quote", _quote_args())
    assert json.loads(first)["saved"] == "q1"
    assert "重复提交" in second
    assert sum(path.endswith("/save") for path, _ in fake.calls) == 1


def test_package_quote_uses_contract_protected_versions(monkeypatch) -> None:
    class ContractClient(FakeClient):
        def post(self, path: str, payload: Any = None) -> Any:
            if path.endswith("/contract/list"):
                self.calls.append((path, payload))
                return [{"id": "contract1"}]
            if "/getContractProtectionPrice/bind/" in path:
                self.calls.append((path, payload))
                return {
                    "accountQuoteVersionDos": [{"id": "protected-price"}],
                    "couponVersionDos": [{"id": "protected-coupon"}],
                }
            return super().post(path, payload)

    fake = ContractClient()
    _use(monkeypatch, fake)
    mcn.call_tool(
        {},
        "youli_create_package_quote",
        _quote_args(customer_opportunity_id="opp1", price_version_id="", coupon_version_id=""),
    )
    payload = next(p for path, p in fake.calls if path.endswith("/save"))
    assert payload["contractId"] == "contract1"
    assert payload["priceVersion"] == "protected-price"
    assert payload["couponVersion"] == "protected-coupon"


def test_package_quote_changed_confirmed_amount_cannot_save(monkeypatch) -> None:
    fake = FakeClient()
    _use(monkeypatch, fake)
    result = mcn.call_tool({}, "youli_create_package_quote", _quote_args(expected_amount=99))
    assert "用户确认金额不同" in result
    assert not any(path.endswith("/save") for path, _ in fake.calls)


def test_package_quote_saves_calculated_discount(monkeypatch) -> None:
    class DiscountClient(FakeClient):
        def post(self, path: str, payload: Any = None) -> Any:
            result = super().post(path, payload)
            if path.endswith("/calculateCoupon"):
                result["orderItems"][0]["couponAfterMoney"] = 80
                result["orderItems"][0]["couponDetailVos"] = [{"couponMoney": 20}]
                result["orderItems"][0]["couponNameList"] = ["折扣"]
            return result

    fake = DiscountClient()
    _use(monkeypatch, fake)
    mcn.call_tool({}, "youli_create_package_quote", _quote_args(expected_amount=80))
    payload = next(p for path, p in fake.calls if path.endswith("/save"))
    assert payload["orderMoney"] == payload["factOrderMoney"] == 80
    item = payload["itemRoList"][0]
    assert item["originalMoney"] == 100
    assert item["couponAfterMoney"] == item["factMoney"] == 80
    assert json.loads(item["couponJson"]) == [{"couponMoney": 20}]
    assert json.loads(item["dynamicItemObj"])["couponNameList"] == ["折扣"]


def test_package_quote_readback_checks_display_and_amount(monkeypatch) -> None:
    class ReadbackClient(FakeClient):
        def get(self, path: str, params: Any = None) -> Any:
            self.calls.append((path, params))
            payload = next(p for path, p in self.calls if path.endswith("/save"))
            return {
                "id": "q1",
                "projectType": payload["projectType"],
                "factOrderMoney": payload["factOrderMoney"],
                "offerOrderItemVos": payload["itemRoList"],
            }

    fake = ReadbackClient()
    _use(monkeypatch, fake)
    result = json.loads(mcn.call_tool({}, "youli_create_package_quote", _quote_args()))
    assert "已核对项目类型" in result["verification"]
    monkeypatch.setattr(fake, "get", lambda *args: {"id": "q1", "offerOrderItemVos": []})
    mcn._SAVED_QUOTES.clear()
    result = json.loads(mcn.call_tool({}, "youli_create_package_quote", _quote_args()))
    assert "回读字段不一致" in result["verification"]


def test_package_quote_custom_account_keeps_custom_type(monkeypatch) -> None:
    class CustomClient(FakeClient):
        def post(self, path: str, payload: Any = None) -> Any:
            result = super().post(path, payload)
            if path.endswith("/product/list"):
                result[0]["attribute"] = 7
            return result

    fake = CustomClient()
    _use(monkeypatch, fake)
    mcn.call_tool({}, "youli_create_package_quote", _quote_args())
    payload = next(p for path, p in fake.calls if path.endswith("/save"))
    assert payload["itemRoList"][0]["type"] == 3
    assert payload["itemRoList"][0]["noCalculate"] == 1


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


def test_customers_use_data_permission(monkeypatch) -> None:
    fake = FakeClient()
    _use(monkeypatch, fake)
    mcn.call_tool({"token": "t"}, "youli_list_customers", {"keyword": "茶颜"})
    path, payload = fake.calls[-1]
    assert path == "/api/mcn/customer-management/list"
    assert payload["name"] == "茶颜"
    assert payload["useDataPermission"] is True


def test_customer_ownerships_use_data_permission(monkeypatch) -> None:
    fake = FakeClient()
    _use(monkeypatch, fake)
    mcn.call_tool(
        {"token": "t"},
        "youli_list_customer_ownerships",
        {"keyword": "茶颜", "customer_id": "c1", "page": 2, "page_size": 50},
    )
    path, payload = fake.calls[-1]
    assert path == "/api/mcn/customer/opportunity/list"
    assert payload["name"] == "茶颜"
    assert payload["customerId"] == "c1"
    assert payload["useDataPermission"] is True
    assert payload["page"] == {"pageNum": 2, "pageSize": 20}


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

def test_combined_order_groups_by_supplier(monkeypatch) -> None:
    class CombinedClient(FakeClient):
        def get(self, path: str, params: Any = None) -> Any:
            self.calls.append((path, params))

            def item(account: str, supplier: str, amount: float) -> dict[str, Any]:
                return {
                    "itemId": account,
                    "type": 0,
                    "skuId": "100",
                    "accountId": account,
                    "quantity": 1,
                    "originalPrice": amount,
                    "originalMoney": amount,
                    "baseMoney": amount,
                    "factMoney": amount,
                    "modeOperation": 1,
                    "dynamicItemObj": json.dumps(
                        {"formLabelAlign": {"supplier": supplier}}
                    ),
                }

            return {
                "customerId": "2",
                "productId": "1",
                "priceVersion": "3",
                "couponVersion": "4",
                "orderType": 1,
                "projectType": 6,
                "customerOpportunityId": "opp1",
                "offerOrderNo": "BJ9",
                "offerOrderItemVos": [item("10", "501", 7500), item("11", "502", 4000)],
            }

    fake = CombinedClient()
    _use(monkeypatch, fake)
    result = json.loads(
        mcn.call_tool(
            {}, "youli_create_combined_order", {"quote_id": "9", "confirm": True}
        )
    )
    posts = [p for path, p in fake.calls if path.endswith("/temporaryStorage")]
    assert result["groups"] == 2
    assert len(posts) == 2
    assert sorted(p["offerOrderSaveRo"]["factOrderMoney"] for p in posts) == [4000.0, 7500.0]
    assert all(len(p["offerOrderSaveRo"]["itemRoList"]) == 1 for p in posts)
    assert all("offerOrderId" not in p for p in posts)
    # 拆单后每张订单都保留项目类型和商机，后续才能按项目统计。
    assert all(p["offerOrderSaveRo"]["projectType"] == 6 for p in posts)
    assert all(p["offerOrderSaveRo"]["customerOpportunityId"] == "opp1" for p in posts)
