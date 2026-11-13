"""Focused tests for the Youli HTTP client wire format."""

from __future__ import annotations

import pytest

from octop.config import YouliConfig
from octop.infra.auth.youli.client import YouliClient, YouliClientError


class _FakeResponse:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body


def _patch_http(monkeypatch, *, status_code: int, body: object, calls: list):
    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

        def post(self, url: str, **kwargs):
            calls.append((url, kwargs))
            return _FakeResponse(status_code, body)

    monkeypatch.setattr("octop.infra.auth.youli.client.httpx.Client", _FakeClient)


def _client() -> YouliClient:
    return YouliClient(
        YouliConfig(
            enabled=True,
            base_url="https://youli.example",
            client_id="octop",
            client_secret="secret",
            audience="octop",
            mdm_base_url="https://mdm.example",
        )
    )


def test_verify_sends_the_token_as_raw_body(monkeypatch) -> None:
    calls: list = []
    _patch_http(
        monkeypatch,
        status_code=200,
        body={"userId": 11, "companyId": 21, "employeeId": 31},
        calls=calls,
    )

    _client().verify("header.payload.signature")

    url, kwargs = calls[0]
    assert url == "https://mdm.example/api/auth/verify"
    # MDM declares @RequestBody String, so a JSON-quoted token would be rejected.
    assert kwargs["content"] == b"header.payload.signature"
    assert "json" not in kwargs
    assert kwargs["headers"]["Content-Type"] == "application/json"


def test_verify_fails_closed_when_identity_is_incomplete(monkeypatch) -> None:
    _patch_http(monkeypatch, status_code=200, body={"userId": 11}, calls=[])

    with pytest.raises(YouliClientError, match="incomplete identity"):
        _client().verify("token")


def test_verify_reports_upstream_rejection(monkeypatch) -> None:
    _patch_http(
        monkeypatch,
        status_code=403,
        body={"code": 401, "msg": "请重新登录"},
        calls=[],
    )

    with pytest.raises(YouliClientError, match="rejected the token"):
        _client().verify("token")


def test_password_login_uses_the_octop_terminal(monkeypatch) -> None:
    calls: list = []
    _patch_http(
        monkeypatch,
        status_code=200,
        body={"code": 200, "data": {"token": "business-token"}},
        calls=calls,
    )

    token = _client().password_login(mobile="15000000088", password="secret")

    assert token == "business-token"
    url, kwargs = calls[0]
    assert url == "https://youli.example/api/crm/user/passwordLogin"
    assert kwargs["json"] == {
        "mobile": "15000000088",
        "password": "secret",
        "terminal": 10,
    }
