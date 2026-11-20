"""Offline protocol tests; these do not exercise integration or persistence."""

from __future__ import annotations

import json

import httpx
import pytest

from octop.infra.feishu_kb.client import FeishuClient, FeishuError, authorize_url


def test_authorization_requests_separate_document_grant() -> None:
    url = httpx.URL(
        authorize_url(client_id="app", redirect_uri="https://local/cb", state="s", challenge="c")
    )
    assert url.host == "accounts.feishu.cn"
    assert url.params["scope"] == "docx:document:readonly offline_access"
    assert url.params["state"] == "s"
    assert url.params["code_challenge_method"] == "S256"


def test_exchange_refresh_and_identity() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/oauth/v3/token":
            body = json.loads(request.content)
            assert request.url.host == "accounts.feishu.cn"
            assert body["client_secret"] == "secret"
            return httpx.Response(
                200, json={"access_token": "access", "refresh_token": "rotated", "expires_in": 3600}
            )
        assert request.url.path == "/open-apis/authen/v1/user_info"
        assert request.headers["Authorization"] == "Bearer access"
        return httpx.Response(
            200, json={"code": 0, "data": {"tenant_key": "tenant", "union_id": "subject"}}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = FeishuClient(http, client_id="app", client_secret="secret")
        grant = client.exchange(code="code", verifier="verifier", redirect_uri="https://local/cb")
        assert json.loads(requests[0].content)["code_verifier"] == "verifier"
        assert client.subject(grant.access_token) == 'feishu-v1:["feishu","app","tenant","subject"]'
        assert client.refresh("old").refresh_token == "rotated"
        assert json.loads(requests[-1].content)["refresh_token"] == "old"
        assert "access" not in repr(grant)
        assert "rotated" not in repr(grant)


def test_refresh_uncertain_result_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("sensitive", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = FeishuClient(http, client_id="app", client_secret="secret")
        with pytest.raises(FeishuError, match="token_unavailable"):
            client.refresh("old")
    assert calls == 1


def test_incomplete_rotated_grant_fails_closed() -> None:
    response = {"access_token": "new", "expires_in": 3600}
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
        ) as http,
        pytest.raises(FeishuError, match="invalid_token_response"),
    ):
        FeishuClient(http, client_id="app", client_secret="secret").refresh("old")


def test_identity_without_tenant_fails_closed() -> None:
    response = {"code": 0, "data": {"union_id": "subject"}}
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
        ) as http,
        pytest.raises(FeishuError, match="invalid_identity"),
    ):
        FeishuClient(http, client_id="app", client_secret="secret").subject("token")


@pytest.mark.parametrize(
    ("status", "code", "reason"),
    [
        (404, 1770002, "missing"),
        (400, 1770003, "missing"),
        (403, 1770032, "forbidden"),
        (401, 0, "forbidden"),
    ],
)
def test_document_unavailable_is_classified(status: int, code: int, reason: str) -> None:
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(status, json={"code": code}))
        ) as http,
        pytest.raises(FeishuError, match=reason),
    ):
        FeishuClient(http, client_id="app", client_secret="secret").raw_content("doc1", "token")


def test_transient_retry_is_bounded_and_reads_content() -> None:
    responses = iter(
        [
            httpx.Response(400, json={"code": 99991400}),
            httpx.Response(503),
            httpx.Response(200, json={"code": 0, "data": {"content": "hello"}}),
        ]
    )
    delays: list[float] = []
    with httpx.Client(transport=httpx.MockTransport(lambda _: next(responses))) as http:
        client = FeishuClient(http, client_id="app", client_secret="secret", sleep=delays.append)
        assert client.raw_content("doc1", "token") == "hello"
    assert delays == [0.5, 1.0]


def test_transient_exhaustion_fails_closed() -> None:
    calls = 0
    delays: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        pytest.raises(FeishuError, match="unavailable"),
    ):
        FeishuClient(
            http, client_id="app", client_secret="secret", sleep=delays.append
        ).raw_content("doc1", "token")
    assert calls == 3
    assert delays == [0.5, 1.0]


def test_document_id_cannot_change_request_path() -> None:
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: pytest.fail("unexpected request"))
        ) as http,
        pytest.raises(FeishuError, match="invalid_document_id"),
    ):
        FeishuClient(http, client_id="app", client_secret="secret").raw_content(
            "../private", "token"
        )
