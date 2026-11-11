"""Unit tests for connector default_open config helpers."""

from __future__ import annotations

from octop.infra.connectors.default_open import (
    build_instance_config_json,
    merge_mcp_servers_with_defaults,
    read_default_open,
)


def test_read_default_open_defaults_false():
    assert read_default_open(None) is False
    assert read_default_open({}) is False
    assert read_default_open({"default_open": False}) is False
    assert read_default_open({"default_open": "yes"}) is False


def test_read_default_open_true():
    assert read_default_open({"default_open": True}) is True


def test_build_instance_config_json_omits_when_false():
    assert build_instance_config_json(kind="tencent-docs", default_open=False) is None
    assert (
        build_instance_config_json(
            kind="qq-mail",
            default_open=False,
            email="a@qq.com",
        )
        == '{"email": "a@qq.com"}'
    )


def test_build_instance_config_json_includes_when_true():
    raw = build_instance_config_json(kind="tencent-docs", default_open=True)
    assert raw == '{"default_open": true}'
    raw_mail = build_instance_config_json(
        kind="qq-mail",
        default_open=True,
        email="a@qq.com",
    )
    assert '"default_open": true' in raw_mail
    assert '"email": "a@qq.com"' in raw_mail


def test_build_instance_config_json_includes_description():
    raw = build_instance_config_json(
        kind="tencent-docs",
        description="团队文档连接",
    )
    assert raw == '{"description": "团队文档连接"}'


def test_merge_mcp_servers_with_defaults_unions_and_dedupes():
    # Default: apply defaults only when explicit is None (IM-style).
    assert merge_mcp_servers_with_defaults(None, ["a", "b"]) == ["a", "b"]
    assert merge_mcp_servers_with_defaults(["b", "c"], ["a", "b"]) == ["b", "c"]
    assert merge_mcp_servers_with_defaults(["x"], []) == ["x"]
    assert merge_mcp_servers_with_defaults(None, []) is None
    assert merge_mcp_servers_with_defaults([], []) is None
    # Cron-style force union.
    assert merge_mcp_servers_with_defaults(["b", "c"], ["a", "b"], apply_defaults=True) == [
        "b",
        "c",
        "a",
    ]


def test_merge_respects_dashboard_opt_out():
    # Explicit list (including empty) must not re-add defaults.
    assert merge_mcp_servers_with_defaults(["b"], ["a"], apply_defaults=False) == ["b"]


def test_merge_turn_mcp_servers_unions_extra_defaults() -> None:
    from octop.infra.connectors.service import ConnectorService

    class _Svc:
        def list_default_open_mcp_server_names(self, _uid: int) -> list[str]:
            return ["always"]

        def list_active_mcp_server_names(self, _uid: int) -> list[str]:
            return ["always", "expert"]

        def expand_connector_refs(self, _uid: int, refs: list[str]) -> list[str]:
            return [name for name in refs if name]

    svc = _Svc()
    assert ConnectorService.merge_turn_mcp_servers(
        svc,  # type: ignore[arg-type]
        1,
        None,
        extra_defaults=["expert", "always", "gone", ""],
    ) == ["always", "expert"]
    assert (
        ConnectorService.merge_turn_mcp_servers(
            svc,  # type: ignore[arg-type]
            1,
            [],
            apply_defaults=False,
            extra_defaults=["expert"],
        )
        is None
    )
    assert merge_mcp_servers_with_defaults([], ["a"], apply_defaults=False) is None
    # IM / Cron force defaults.
    assert merge_mcp_servers_with_defaults([], ["a"], apply_defaults=True) == ["a"]
    assert merge_mcp_servers_with_defaults(None, ["a"], apply_defaults=True) == ["a"]


def test_expand_connector_refs_uses_the_caller_own_instances() -> None:
    from types import SimpleNamespace

    from octop.infra.connectors.service import ConnectorService

    class _Repo:
        def list_visible(self, _uid: int):
            return [
                SimpleNamespace(
                    kind="mcn",
                    status="active",
                    has_credentials=True,
                    mcp_server_name="mcn__a",
                    user_id=1,
                ),
                SimpleNamespace(
                    kind="mcn",
                    status="active",
                    has_credentials=False,
                    mcp_server_name="mcn__b",
                    user_id=1,
                ),
                SimpleNamespace(
                    kind="mcn",
                    status="active",
                    has_credentials=True,
                    mcp_server_name="mcn__a",
                    user_id=1,
                ),
                SimpleNamespace(
                    kind="mcn",
                    status="active",
                    has_credentials=True,
                    mcp_server_name="mcn__shared",
                    user_id=2,
                ),
                SimpleNamespace(
                    kind="erp", status="active", has_credentials=True, mcp_server_name="erp__c"
                ),
            ]

    svc = ConnectorService.__new__(ConnectorService)
    svc._repo = _Repo()  # type: ignore[assignment]
    expanded = ConnectorService.expand_connector_refs(svc, 1, ["mcn", "mcn__x", "", "gone"])
    assert expanded == ["mcn__a", "mcn__x"]
    assert "mcn__shared" not in expanded
