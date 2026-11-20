"""Focused tests for the Youli HTTP client wire format."""

from __future__ import annotations

import pytest

from octop.config import YouliConfig
from octop.infra.auth.youli import http_pool
from octop.infra.auth.youli.client import (
    YouliClient,
    YouliClientError,
    YouliClientUnavailable,
)


class _FakeResponse:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body
        self.content = b"" if body is None else b"{}"
        self.text = "" if body is None else "{}"

    def json(self) -> object:
        if self._body is None:
            raise ValueError("empty body")
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

    monkeypatch.setattr(http_pool.httpx, "Client", _FakeClient)


def _patch_http_seq(monkeypatch, responses: list[tuple[int, object]], calls: list):
    queue = list(responses)

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

        def request(self, method: str, url: str, **kwargs):
            calls.append((method, url, kwargs))
            status_code, body = queue.pop(0)
            return _FakeResponse(status_code, body)

    monkeypatch.setattr("octop.infra.auth.youli.client.httpx.Client", _FakeClient)


def _client(*, inner_api_key: str = "") -> YouliClient:
    return YouliClient(
        YouliConfig(
            enabled=True,
            base_url="https://youli.example",
            client_id="octop",
            client_secret="secret",
            audience="octop",
            mdm_base_url="https://mdm.example",
            inner_api_key=inner_api_key,
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


@pytest.mark.parametrize("terminal", [10, 1_000_000_001])
def test_issue_delegation_uses_only_the_isolated_endpoint(monkeypatch, terminal) -> None:
    calls: list = []
    _patch_http(
        monkeypatch,
        status_code=200,
        body={
            "code": 200,
            "data": {
                "token": "test-delegation",
                "userId": 11,
                "companyId": 21,
                "employeeId": 31,
                "terminal": terminal,
            },
        },
        calls=calls,
    )
    if terminal == 10:
        with pytest.raises(YouliClientError, match="invalid delegation terminal"):
            _client().issue_delegation(mobile="test-user", password="test-password")
    else:
        result = _client().issue_delegation(mobile="test-user", password="test-password")
        assert (result.company_id, result.employee_id, result.terminal) == (21, 31, terminal)
    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == "https://youli.example/api/crm/sso/youli/delegation"
    assert kwargs["json"] == {
        "clientId": "octop",
        "clientSecret": "secret",
        "audience": "octop",
        "mobile": "test-user",
        "password": "test-password",
    }


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


def test_inner_login_by_phone_sends_key_and_parses_ok(monkeypatch) -> None:
    calls: list = []
    _patch_http_seq(
        monkeypatch,
        responses=[
            (
                200,
                {
                    "code": 200,
                    "data": {
                        "status": "ok",
                        "token": "biz-jwt",
                        "username": "alice",
                        "nickName": "Alice",
                        "mobile": "13800001234",
                        "userId": 11,
                        "companyId": 21,
                        "employeeId": 31,
                        "industry": 5,
                        "terminal": 10,
                    },
                },
            )
        ],
        calls=calls,
    )

    res = _client(inner_api_key="secret-key").inner_login_by_phone(phone="13800001234")

    assert res.status == "ok"
    assert res.issued is not None
    assert res.issued.token == "biz-jwt"
    assert res.issued.company_id == 21
    assert res.issued.employee_id == 31
    assert res.issued.terminal == 10
    method, url, kwargs = calls[0]
    assert method == "POST"
    assert url == "https://mdm.example/api/auth/inner/loginByPhone"
    assert kwargs["headers"] == {"X-Inner-Api-Key": "secret-key"}
    assert kwargs["json"] == {"phone": "13800001234", "terminal": 10}


@pytest.mark.parametrize(
    ("status", "reason"),
    [("not_found", "erp_not_found"), ("blocked", "erp_disabled")],
)
def test_inner_login_by_employee_handles_not_found_and_blocked(
    monkeypatch, status: str, reason: str
) -> None:
    calls: list = []
    _patch_http_seq(
        monkeypatch,
        responses=[
            (
                200,
                {
                    "code": 200,
                    "data": {"status": status, "reason": reason, "message": "blocked"},
                },
            )
        ],
        calls=calls,
    )

    res = _client().inner_login_by_employee(company_id=21, employee_id=31)
    assert res.status == status
    assert res.reason == reason
    assert res.issued is None
    assert calls[0][1] == "https://mdm.example/api/auth/inner/loginByEmployee"


def test_inner_login_by_phone_404_falls_back_to_legacy_mdm(monkeypatch) -> None:
    calls: list = []
    _patch_http_seq(
        monkeypatch,
        responses=[
            (404, {"code": 404}),
            (200, {"id": 11, "userName": "alice", "phone": "13800001234", "status": 1}),
            (200, {"id": 31, "companyId": 21, "nickName": "Alice", "status": 1}),
            (200, {"companyId": 21, "userId": 31, "deleted": 0}),
        ],
        calls=calls,
    )

    res = _client().inner_login_by_phone(phone="13800001234")
    assert res.status == "ok_no_token"
    assert res.company_id == 21
    assert res.employee_id == 31
    assert res.user_id == 11
    assert [c[1] for c in calls] == [
        "https://mdm.example/api/auth/inner/loginByPhone",
        "https://mdm.example/api/account/findByPhone",
        "https://mdm.example/api/user/findDefaultUserByPhone",
        "https://mdm.example/api/user/getLoginUser",
    ]


def test_inner_login_fails_closed_on_mdm_http_error(monkeypatch) -> None:
    _patch_http_seq(
        monkeypatch,
        responses=[(500, {"code": 500, "msg": "internal error"})],
        calls=[],
    )

    with pytest.raises(YouliClientError, match="internal error"):
        _client().inner_login_by_phone(phone="13800001234")


def test_inner_login_fails_closed_on_transport_error(monkeypatch) -> None:
    import httpx

    class _FailingClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

        def request(self, method: str, url: str, **kwargs):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("octop.infra.auth.youli.client.httpx.Client", _FailingClient)

    with pytest.raises(YouliClientUnavailable, match="unreachable"):
        _client().inner_login_by_phone(phone="13800001234")
