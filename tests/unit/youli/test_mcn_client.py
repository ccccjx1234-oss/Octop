"""Wire-format and error-mapping tests for the MCN business client."""

from __future__ import annotations

import httpx
import pytest

from octop.config import YouliConfig
from octop.infra.auth.youli.mcn_client import (
    McnAuthExpired,
    McnClient,
    McnClientError,
)


class _FakeResponse:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body


def _patch_http(monkeypatch, *, status_code=200, body=None, calls=None, error=None):
    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

        def request(self, method, url, *, json=None, params=None, headers=None):
            if error is not None:
                raise error
            assert calls is not None
            calls.append((method, url, json, params, headers))
            return _FakeResponse(status_code, body)

    monkeypatch.setattr(
        "octop.infra.auth.youli.mcn_client.httpx.Client", _FakeClient
    )


def _client() -> McnClient:
    return McnClient(YouliConfig(mcn_base_url="http://mcn.example"), "business-token")


def test_post_sends_business_token_and_unwraps_data(monkeypatch) -> None:
    calls: list = []
    _patch_http(monkeypatch, body={"code": 200, "data": {"items": [1]}}, calls=calls)

    data = _client().post("/api/mcn/account/list", {"page": {"pageNum": 1}})

    assert data == {"items": [1]}
    method, url, payload, params, headers = calls[0]
    assert method == "POST"
    assert url == "http://mcn.example/api/mcn/account/list"
    assert payload == {"page": {"pageNum": 1}}
    assert params is None
    assert headers == {"Auth-Token": "business-token"}


def test_auth_rejection_is_reported_as_expired(monkeypatch) -> None:
    _patch_http(monkeypatch, body={"code": 403, "msg": "请重新登录"}, calls=[])

    with pytest.raises(McnAuthExpired, match="请重新登录"):
        _client().get("/api/mcn/offer/order/detail", {"id": "1"})


def test_business_error_is_reported(monkeypatch) -> None:
    _patch_http(monkeypatch, body={"code": 500, "msg": "报价单不存在"}, calls=[])

    with pytest.raises(McnClientError, match="报价单不存在"):
        _client().post("/api/mcn/offer/order/save", {})


def test_unreachable_is_reported(monkeypatch) -> None:
    _patch_http(monkeypatch, error=httpx.ConnectError("boom"))

    with pytest.raises(McnClientError, match="不可达"):
        _client().post("/api/mcn/account/list", {})


def test_unconfigured_client_fails_closed() -> None:
    with pytest.raises(McnClientError, match="未配置"):
        McnClient(YouliConfig(), "token").post("/api/mcn/account/list", {})
