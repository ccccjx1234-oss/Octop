"""MCN connector privacy: no sharing, no cross-employee token, no wiped credential."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from octop.infra.connectors.builder import personal_connector_visible
from octop.infra.connectors.service import ConnectorService
from octop.infra.errors import OctopError


def _instance(**kwargs):
    defaults = {
        "kind": "mcn",
        "status": "active",
        "has_credentials": True,
        "credential_blob": b"blob",
        "mcp_server_name": "mcn__own",
        "user_id": 1,
        "instance_id": "own",
        "display_name": "MCN",
        "shared": False,
        "created_at": 1,
        "updated_at": 1,
        "config_json": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class _Repo:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def connector_feature_allows(self, _user_id: int) -> bool:
        return True

    def list_visible(self, _user_id: int):
        return list(self._rows)

    def get_by_user_kind(self, _user_id: int, _kind: str):
        return None

    def list_by_kind(self, _kind: str):
        return []

    def get(self, instance_id: str):
        for row in self._rows:
            if row.instance_id == instance_id:
                return row
        return None

    def list_by_user(self, user_id: int):
        return [row for row in self._rows if row.user_id == user_id]


def _service(repo: _Repo, sessions=None) -> ConnectorService:
    svc = ConnectorService.__new__(ConnectorService)
    svc._repo = repo
    svc._secret_repo = object()
    svc._youli_session_repo = sessions
    svc.get_custom_servers = lambda _user_id: {}
    return svc


def test_personal_connector_visible_only_for_mcn_owner() -> None:
    own = _instance(user_id=1)
    other = _instance(user_id=2, mcp_server_name="mcn__other")
    shared_other_kind = _instance(kind="notion", user_id=2, mcp_server_name="notion__x")
    assert personal_connector_visible(own, 1) is True
    assert personal_connector_visible(other, 1) is False
    assert personal_connector_visible(shared_other_kind, 1) is True


def test_turn_defaults_combine_employee_kind_and_shared_custom_alias() -> None:
    svc = _service(_Repo([_instance()]))
    svc.list_active_mcp_server_names = lambda _uid: ["mcn__own", "doris", "custom__owner__doris"]
    svc.shared_custom_harness_aliases = lambda _uid: {"custom__owner__doris": "doris"}
    assert svc.merge_turn_mcp_servers(
        1, None, extra_defaults=["mcn", "doris"], agent_owner_id=2
    ) == ["mcn__own", "custom__owner__doris"]
    assert svc.merge_turn_mcp_servers(1, ["doris", "mcn"]) == ["doris", "mcn__own"]


def test_active_names_and_api_list_hide_another_employees_mcn() -> None:
    repo = _Repo(
        [
            _instance(),
            _instance(user_id=2, instance_id="other", mcp_server_name="mcn__other", shared=True),
            _instance(
                kind="notion",
                user_id=2,
                instance_id="notion",
                mcp_server_name="notion__x",
                shared=True,
            ),
        ]
    )
    svc = _service(repo)
    assert svc.list_active_mcp_server_names(1) == ["mcn__own", "notion__x"]
    listed = {item["mcp_server_name"] for item in svc.list_instances_for_api(1)}
    assert listed == {"mcn__own", "notion__x"}


def test_decrypt_keeps_stored_token_when_employee_has_no_live_session(monkeypatch) -> None:
    monkeypatch.setattr(
        "octop.infra.connectors.service.decrypt_credentials",
        lambda _repo, _blob: {"token": "stored", "internal_token": "internal"},
    )
    svc = _service(
        _Repo([_instance()]),
        sessions=SimpleNamespace(list_active_for_user=lambda _user_id: []),
    )
    creds = svc.decrypt("own")
    assert creds["token"] == "stored"


def test_decrypt_uses_the_employees_newest_live_token(monkeypatch) -> None:
    monkeypatch.setattr(
        "octop.infra.connectors.service.decrypt_credentials",
        lambda _repo, _blob: {"token": "stored"},
    )
    monkeypatch.setattr(
        "octop.infra.connectors.service.decrypt_secret",
        lambda _repo, _blob: "live-token",
    )
    svc = _service(
        _Repo([_instance()]),
        sessions=SimpleNamespace(
            list_active_for_user=lambda _user_id: [SimpleNamespace(token_blob=b"enc")]
        ),
    )
    assert svc.decrypt("own")["token"] == "live-token"


def test_sid_token_is_refused_for_another_employees_connector(monkeypatch) -> None:
    monkeypatch.setattr(
        "octop.infra.connectors.service.decrypt_secret",
        lambda _repo, _blob: "employee-two",
    )
    session = SimpleNamespace(user_id=2, token_blob=b"enc")
    svc = _service(
        _Repo([_instance(user_id=2, instance_id="two")]),
        sessions=SimpleNamespace(get_active=lambda _sid: session),
    )
    assert svc.live_youli_token_for_sid("sid-2", owner_user_id=1) == ""
    assert svc.live_youli_token_for_sid("sid-2", owner_user_id=2) == "employee-two"
    assert svc.live_youli_token_for_sid("sid-2") == "employee-two"


def test_shared_mcn_connector_is_rejected() -> None:
    from octop.api.routers.connectors import _reject_shared_mcn

    _reject_shared_mcn("notion", True)
    _reject_shared_mcn("mcn", False)
    with pytest.raises(OctopError, match="不能共享"):
        _reject_shared_mcn("mcn", True)


@pytest.mark.asyncio
async def test_create_and_patch_refuse_to_share_mcn(monkeypatch) -> None:
    from octop.api.routers import connectors

    created = {"called": False}

    class _CreateRepo:
        def name_exists(self, *_args, **_kwargs) -> bool:
            return False

        def get(self, instance_id: str):
            return _instance(instance_id=instance_id, user_id=1)

        def create(self, **_kwargs) -> None:
            created["called"] = True

        def update_metadata(self, *_args, **_kwargs) -> None:
            created["called"] = True

    repo = _CreateRepo()
    server = SimpleNamespace(
        services=SimpleNamespace(
            secret_repo=object(),
            settings_repo=object(),
            config=SimpleNamespace(),
            repos=SimpleNamespace(connector_repo=repo, youli_session_repo=None),
            audit_repo=SimpleNamespace(write=lambda **_kwargs: None),
        )
    )
    user = SimpleNamespace(id=1, username="owner", role="user")
    monkeypatch.setattr(connectors, "_custom_name_exists", lambda *_args, **_kwargs: False)

    with pytest.raises(OctopError, match="不能共享"):
        await connectors.create_instance(
            connectors.CreateInstanceBody(
                kind="mcn",
                display_name="MCN",
                description="员工自己的 MCN",
                shared=True,
                credentials={"token": "employee-token"},
            ),
            SimpleNamespace(),
            user,
            server,
        )
    with pytest.raises(OctopError, match="不能共享"):
        await connectors.patch_instance(
            "own",
            connectors.PatchInstanceBody(visibility="all"),
            user,
            server,
        )
    assert created["called"] is False


@pytest.mark.asyncio
async def test_mcp_configs_skip_another_employees_mcn(monkeypatch) -> None:
    async def fresh(self, instance_id: str, kind: str):
        del self, kind
        return {"token": "t", "internal_token": "i"}

    async def fresh_custom(self, user_id: int):
        del self, user_id
        return None

    monkeypatch.setattr(ConnectorService, "ensure_fresh_credentials", fresh)
    monkeypatch.setattr(ConnectorService, "ensure_fresh_custom_servers", fresh_custom)
    monkeypatch.setattr(
        "octop.infra.connectors.service.build_http_mcp_spec",
        lambda **kwargs: {"instance": kwargs["instance_id"]},
    )
    svc = _service(
        _Repo(
            [
                _instance(),
                _instance(
                    user_id=2, instance_id="other", mcp_server_name="mcn__other", shared=True
                ),
                _instance(
                    kind="dify",
                    user_id=2,
                    instance_id="dify",
                    mcp_server_name="dify__x",
                    shared=True,
                ),
            ]
        )
    )
    svc._config = SimpleNamespace()
    configs = await svc.mcp_configs_for_user(1)
    assert set(configs) == {"mcn__own", "dify__x"}


@pytest.mark.asyncio
async def test_internal_mcp_does_not_wipe_token_when_live_session_is_missing(monkeypatch) -> None:
    from octop.api.routers import internal_mcp

    seen: dict[str, object] = {}

    def handle(*, kind, creds, body):
        del kind, body
        seen["token"] = creds["token"]
        return {"jsonrpc": "2.0", "id": 1, "result": {}}

    monkeypatch.setattr(internal_mcp, "handle_mcp_request", handle)
    monkeypatch.setattr(internal_mcp, "_live_mcn_token", lambda _server, _user_id: "")
    monkeypatch.setattr(
        internal_mcp,
        "_service",
        lambda _server: SimpleNamespace(
            verify_internal_token=lambda _instance_id, _token: {"token": "stored"}
        ),
    )
    server = SimpleNamespace(
        services=SimpleNamespace(
            repos=SimpleNamespace(
                connector_repo=SimpleNamespace(get=lambda _instance_id: _instance(status="active"))
            )
        )
    )

    class _Request:
        headers: dict[str, str] = {}

        async def json(self):
            return {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

    await internal_mcp.internal_mcp_post("mcn", "own", _Request(), token="internal", server=server)
    assert seen["token"] == "stored"


@pytest.mark.asyncio
async def test_internal_mcp_replaces_token_with_the_live_employee_token(monkeypatch) -> None:
    from octop.api.routers import internal_mcp

    seen: dict[str, object] = {}

    def handle(*, kind, creds, body):
        del kind, body
        seen["token"] = creds["token"]
        return {"jsonrpc": "2.0", "id": 1, "result": {}}

    monkeypatch.setattr(internal_mcp, "handle_mcp_request", handle)
    monkeypatch.setattr(internal_mcp, "_live_mcn_token", lambda _server, _user_id: "live-token")
    monkeypatch.setattr(
        internal_mcp,
        "_service",
        lambda _server: SimpleNamespace(
            verify_internal_token=lambda _instance_id, _token: {"token": "stored"}
        ),
    )
    server = SimpleNamespace(
        services=SimpleNamespace(
            repos=SimpleNamespace(
                connector_repo=SimpleNamespace(get=lambda _instance_id: _instance(status="active"))
            )
        )
    )

    class _Request:
        headers: dict[str, str] = {}

        async def json(self):
            return {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}

    await internal_mcp.internal_mcp_post("mcn", "own", _Request(), token="internal", server=server)
    assert seen["token"] == "live-token"
