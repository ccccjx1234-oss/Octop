from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage

from octop.infra.agents.middleware import model_failover
from octop.infra.agents.middleware.model_failover import SafeModelFailoverMiddleware
from octop.infra.agents.providers.fallback_chain import (
    load_fallback_chain,
    save_fallback_chain,
)
from octop.infra.agents.providers.health import FailureScope, ModelHealthRegistry
from octop.infra.errors import ErrorCode, OctopError


def _model(ref: str) -> SimpleNamespace:
    provider, _, model = ref.partition("/")
    return SimpleNamespace(model=ref, provider=provider, model_name=model, profile={})


def _middleware(
    health: ModelHealthRegistry,
    models: dict[str, SimpleNamespace],
    chain: list[str],
    *,
    ref_allowed=None,
):
    return SafeModelFailoverMiddleware(
        health=health,
        candidates=lambda _current, _vision: [],
        model_for_ref=models.__getitem__,
        max_retries=0,
        initial_delay=0,
        max_delay=0,
        on_failure="error",
        fallback_refs=lambda: chain,
        ref_allowed=ref_allowed,
    )


@pytest.mark.asyncio
async def test_pinned_model_switches_to_fallback_chain(monkeypatch) -> None:
    models = {"p/a": _model("p/a"), "q/b": _model("q/b")}
    configurable = {"model": "p/a"}
    health = ModelHealthRegistry(clock=lambda: 100.0)
    middleware = _middleware(health, models, ["q/b"])
    monkeypatch.setattr(model_failover, "_configurable", lambda _request: configurable)
    seen: list[str] = []

    async def handler(request: ModelRequest) -> ModelResponse:
        seen.append(request.model.model)
        if request.model.model == "p/a":
            raise RuntimeError("HTTP 503 service unavailable")
        return ModelResponse(result=[AIMessage(content="ok")])

    response = await middleware.awrap_model_call(
        ModelRequest(model=models["p/a"], messages=[HumanMessage(content="hi")]), handler
    )

    assert response.result[0].content == "ok"
    assert seen == ["p/a", "q/b"]
    assert configurable["model"] == "q/b"


def test_pinned_model_without_chain_still_fails(monkeypatch) -> None:
    models = {"p/a": _model("p/a")}
    health = ModelHealthRegistry(clock=lambda: 100.0)
    middleware = _middleware(health, models, [])
    monkeypatch.setattr(model_failover, "_configurable", lambda _request: {"model": "p/a"})

    def handler(_request: ModelRequest) -> ModelResponse:
        raise RuntimeError("HTTP 503 service unavailable")

    with pytest.raises(RuntimeError):
        middleware.wrap_model_call(
            ModelRequest(model=models["p/a"], messages=[HumanMessage(content="hi")]), handler
        )


def test_pinned_model_does_not_switch_on_request_error(monkeypatch) -> None:
    models = {"p/a": _model("p/a"), "q/b": _model("q/b")}
    health = ModelHealthRegistry(clock=lambda: 100.0)
    middleware = _middleware(health, models, ["q/b"])
    monkeypatch.setattr(model_failover, "_configurable", lambda _request: {"model": "p/a"})
    seen: list[str] = []

    def handler(request: ModelRequest) -> ModelResponse:
        seen.append(request.model.model)
        raise ValueError("invalid tool schema")

    with pytest.raises(ValueError):
        middleware.wrap_model_call(
            ModelRequest(model=models["p/a"], messages=[HumanMessage(content="hi")]), handler
        )
    assert seen == ["p/a"]


def test_availability_window_counts_recent_outcomes() -> None:
    now = [0.0]
    health = ModelHealthRegistry(clock=lambda: now[0], availability_window_s=60)
    health.record_success("p/a")
    health.record_failure("p/a", kind="timeout", scope=FailureScope.MODEL)
    assert health.availability("p/a")["calls"] == 2
    assert health.availability("p/a")["success_rate"] == 0.5

    now[0] = 120.0
    health.record_success("p/a")
    stats = health.availability("p/a")
    assert stats["calls"] == 1
    assert stats["success_rate"] == 1.0
    assert [row["ref"] for row in health.overview()] == ["p/a"]


class _Settings:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str) -> None:
        self.data[key] = value


def test_fallback_chain_round_trip_and_validation() -> None:
    settings = _Settings()
    assert load_fallback_chain(settings) == []
    assert save_fallback_chain(settings, [" p/a ", "p/a", "q/b"]) == ["p/a", "q/b"]
    assert load_fallback_chain(settings) == ["p/a", "q/b"]
    with pytest.raises(OctopError):
        save_fallback_chain(settings, ["no-provider"])


def test_unavailable_chain_ref_is_skipped(monkeypatch) -> None:
    models = {"p/a": _model("p/a")}
    health = ModelHealthRegistry(clock=lambda: 100.0)
    middleware = _middleware(health, models, ["gone/model"])
    monkeypatch.setattr(model_failover, "_configurable", lambda _request: {"model": "p/a"})

    response = middleware.wrap_model_call(
        ModelRequest(model=models["p/a"], messages=[HumanMessage(content="hi")]),
        lambda _req: ModelResponse(result=[AIMessage(content="ok")]),
    )

    assert response.result[0].content == "ok"


def test_chain_ref_denied_by_user_grant_is_not_tried(monkeypatch) -> None:
    models = {"p/a": _model("p/a"), "q/b": _model("q/b")}
    health = ModelHealthRegistry(clock=lambda: 100.0)
    middleware = _middleware(health, models, ["q/b"], ref_allowed=lambda _ref: False)
    monkeypatch.setattr(model_failover, "_configurable", lambda _request: {"model": "p/a"})
    seen: list[str] = []

    def handler(request: ModelRequest) -> ModelResponse:
        seen.append(request.model.model)
        raise RuntimeError("HTTP 503 service unavailable")

    with pytest.raises(RuntimeError):
        middleware.wrap_model_call(
            ModelRequest(model=models["p/a"], messages=[HumanMessage(content="hi")]), handler
        )
    assert seen == ["p/a"]


def test_save_chain_rejects_unusable_ref() -> None:
    class Settings:
        value: str | None = None

        def get(self, _key: str) -> str | None:
            return self.value

        def set(self, _key: str, value: str) -> None:
            self.value = value

    with pytest.raises(OctopError) as excinfo:
        save_fallback_chain(Settings(), ["p/a"], is_usable=lambda _ref: False)
    assert excinfo.value.code is ErrorCode.INVALID_ARGUMENT
