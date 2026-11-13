"""Identity, protocol, sharing and request isolation regression checks."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from octop_harness.backends.workspace import BackendWorkspace

from octop.config import OctopConfig
from octop.infra.db.repos.agents import AgentRow
from octop.infra.errors import OctopError
from octop.infra.isolation.backend import UserWorkerBackend
from octop.infra.isolation.codec import decode, encode
from octop.infra.isolation.context import actor_id, actor_scope, enabled, invocation_scope
from octop.infra.isolation.policy import IsolationPolicy, WorkerIdentity
from octop.infra.isolation.runtimes import UserRuntimes, runtime_key
from octop.infra.isolation.sharing import FileSharing, share_path
from octop.infra.utils.paths import PathLayout


@pytest.fixture(autouse=True)
def _reset_isolation_mode():
    enabled.cache_clear()
    yield
    enabled.cache_clear()


def test_codec_preserves_bytes_and_rejects_objects() -> None:
    value = [b"\x00\xff", {"file": b"hello"}, None]
    assert decode(encode(value)) == value
    with pytest.raises(TypeError):
        encode(object())


@pytest.mark.asyncio
async def test_actor_context_survives_threads_without_crossing_concurrent_tasks() -> None:
    async def check(uid: int) -> int | None:
        with actor_scope(uid):
            await asyncio.sleep(0)
            assert actor_id() == uid
            return await asyncio.to_thread(actor_id)

    assert await asyncio.gather(check(1), check(2)) == [1, 2]
    assert actor_id() is None


def test_invalid_environment_does_not_silently_disable_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OCTOP_USER_ISOLATION", "tru")
    with pytest.raises(RuntimeError):
        enabled()


def test_live_environment_reload_cannot_partially_enable_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OCTOP_USER_ISOLATION", "0")
    assert enabled() is False
    monkeypatch.setenv("OCTOP_USER_ISOLATION", "1")
    assert enabled() is False
    enabled.cache_clear()  # Simulate a new supervisor process only in the test.
    assert enabled() is True


def test_worker_never_inherits_service_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_PASSWORD", "must-not-leak")
    monkeypatch.setenv("LD_PRELOAD", "must-not-load")
    policy = IsolationPolicy(PathLayout(tmp_path))
    identity = WorkerIdentity(1, 200001, tmp_path / "files", tmp_path / "state")
    env = policy.environment(identity)
    assert "DATABASE_PASSWORD" not in env
    assert "LD_PRELOAD" not in env
    policy.setpriv = "setpriv"
    policy.state = "ready"
    argv = policy.command(identity, ["python", "worker.py"])
    assert {
        "--clear-groups",
        "--bounding-set=-all",
        "--ambient-caps=-all",
        "--no-new-privs",
    } <= set(argv)


def test_worker_environment_follows_legacy_system_file_layout(tmp_path: Path) -> None:
    policy = IsolationPolicy(PathLayout(tmp_path))
    identity = WorkerIdentity(1, 200001, tmp_path / "files", tmp_path / "state", "")
    environment = policy.environment(identity)
    assert environment["OCTOP_SKILLS_DIR"] == str(tmp_path / "files" / "skills")
    assert environment["OCTOP_AUTH_DIR"] == str(tmp_path / "files" / ".octop-auth")


def _runtimes(tmp_path: Path) -> tuple[UserRuntimes, AgentRow]:
    source = AgentRow(
        id=1,
        agent_id="COMMON",
        user_id=1,
        name="Common expert",
        description=None,
        persona_mbti=None,
        default_model=None,
        system_prompt="shared instructions",
        enabled=1,
        config_json="{}",
        last_state="running",
        last_error=None,
        created_at=0,
        updated_at=0,
        is_shared=1,
    )
    repos = SimpleNamespace(
        agent_repo=SimpleNamespace(get=lambda _: source),
        user_repo=SimpleNamespace(get=lambda uid: SimpleNamespace(role="user")),
    )
    policy = MagicMock()
    policy.root = tmp_path
    policy.identity.side_effect = lambda uid, key, **kwargs: WorkerIdentity(
        uid,
        200000 + uid,
        tmp_path / "files" / str(uid) / key,
        tmp_path / "state" / key,
        kwargs.get("system_files_path", ".octop"),
    )
    manager = SimpleNamespace(
        _repos=repos,
        _agent_config_dict=lambda row: {},
        _harness_manager=None,
        octop_config=OctopConfig(),
    )
    return UserRuntimes(manager, policy), source


def test_shared_definition_has_private_files_memory_and_stable_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtimes, source = _runtimes(tmp_path)
    monkeypatch.setattr("octop.infra.isolation.runtimes.expert_visible_to", lambda *a, **kw: True)
    first = runtimes.row(source.agent_id, 1)
    second = runtimes.row(source.agent_id, 2)
    assert first.agent_id != second.agent_id
    assert first.config_json != second.config_json
    assert first.system_prompt == second.system_prompt == source.system_prompt
    assert runtimes.row(source.agent_id, 1).agent_id == first.agent_id
    assert runtimes.backend(first).identity.uid != runtimes.backend(second).identity.uid
    assert source.is_shared == 1


def test_shared_expert_never_defaults_to_creator(tmp_path: Path) -> None:
    runtimes, source = _runtimes(tmp_path)
    with pytest.raises(OctopError):
        runtimes.row(source.agent_id)


def test_audience_changes_do_not_reset_owner_memory_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtimes, source = _runtimes(tmp_path)
    monkeypatch.setattr("octop.infra.isolation.runtimes.expert_visible_to", lambda *a, **kw: True)
    shared_row = runtimes.row(source.agent_id, 1)
    namespace = runtimes.namespace(shared_row)
    prefix = json.loads(shared_row.config_json)["system_files_path"]
    private_source = replace(source, is_shared=0)
    runtimes.manager._repos.agent_repo.get = lambda _: private_source
    private_row = runtimes.row(source.agent_id, 1)
    assert runtimes.namespace(private_row) == namespace
    assert json.loads(private_row.config_json)["system_files_path"] == prefix


def test_visibility_revocation_applies_to_existing_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtimes, source = _runtimes(tmp_path)
    monkeypatch.setattr("octop.infra.isolation.runtimes.expert_visible_to", lambda *a, **kw: True)
    runtimes.row(source.agent_id, 2)
    monkeypatch.setattr("octop.infra.isolation.runtimes.expert_visible_to", lambda *a, **kw: False)
    with pytest.raises(OctopError):
        runtimes.row(source.agent_id, 2)


def test_runtime_names_are_valid_memory_namespaces() -> None:
    assert runtime_key("expert/with spaces", 7).isalnum()
    assert runtime_key("one", 7) != runtime_key("two", 7)


def test_invocation_cannot_claim_another_actor_or_thread() -> None:
    manager = SimpleNamespace(
        _user_runtimes=SimpleNamespace(
            source_row=lambda _: SimpleNamespace(agent_id="COMMON", user_id=1, is_shared=True)
        ),
        _repos=SimpleNamespace(
            thread_repo=SimpleNamespace(get=lambda _: SimpleNamespace(user_id=2, agent_id="COMMON"))
        ),
    )
    with actor_scope(1):
        with pytest.raises(OctopError), invocation_scope(manager, "COMMON", {"user": "2"}):
            pass
        with (
            pytest.raises(OctopError),
            invocation_scope(manager, "COMMON", {"user": "1", "thread_id": "other"}),
        ):
            pass


def test_unattended_shared_invocation_uses_explicit_user_not_creator() -> None:
    manager = SimpleNamespace(
        _user_runtimes=SimpleNamespace(
            source_row=lambda _: SimpleNamespace(agent_id="COMMON", user_id=1, is_shared=True)
        ),
        _repos=SimpleNamespace(thread_repo=SimpleNamespace(get=lambda _: None)),
    )
    with invocation_scope(manager, "COMMON", {"user": "2"}):
        assert actor_id() == 2
    assert actor_id() is None
    with pytest.raises(OctopError), invocation_scope(manager, "COMMON", {}):
        pass


@pytest.mark.asyncio
async def test_gateway_resume_binds_its_job_actor_before_touching_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from octop.infra.gateway.process.processor import GlobalProcessor

    monkeypatch.setenv("OCTOP_USER_ISOLATION", "1")
    manager = SimpleNamespace(
        _user_runtimes=SimpleNamespace(
            source_row=lambda _: SimpleNamespace(agent_id="COMMON", user_id=1, is_shared=True)
        ),
        _repos=SimpleNamespace(
            thread_repo=SimpleNamespace(get=lambda _: SimpleNamespace(user_id=2, agent_id="COMMON"))
        ),
        ensure_user_runtime=AsyncMock(),
    )
    reached: list[int | None] = []

    async def inner(**kwargs):
        reached.append(actor_id())
        yield {"type": "ok"}

    processor = SimpleNamespace(_agent_manager=manager, _iter_hitl_resume_with_actor=inner)
    frames = [
        chunk
        async for chunk in GlobalProcessor.iter_hitl_resume_chunks(
            processor, agent_id="COMMON", thread_id="owned", user_id=2, decisions=[]
        )
    ]
    assert frames == [{"type": "ok"}]
    assert reached == [2]
    assert actor_id() is None
    manager._repos.thread_repo.get = lambda _: SimpleNamespace(user_id=1, agent_id="COMMON")
    with pytest.raises(OctopError):
        [
            chunk
            async for chunk in GlobalProcessor.iter_hitl_resume_chunks(
                processor, agent_id="COMMON", thread_id="foreign", user_id=2, decisions=[]
            )
        ]
    assert reached == [2]


@pytest.mark.asyncio
async def test_concurrent_first_use_initializes_one_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtimes, source = _runtimes(tmp_path)
    monkeypatch.setattr(
        "octop.infra.isolation.migration.migrate_owner_workspace", lambda *args: None
    )
    monkeypatch.setattr("octop.infra.isolation.runtimes.expert_visible_to", lambda *a, **kw: True)
    entries: dict[str, object] = {}
    fake_agent = object()

    async def create(cfg: object, *, agent_id: str, init_workspace: bool) -> object:
        await asyncio.sleep(0.01)
        entry = SimpleNamespace(agent=fake_agent)
        entries[agent_id] = entry
        return entry

    def load(key: str) -> object:
        return entries[key]

    harness = SimpleNamespace(
        shared_factory=object(), get_agent=load, acreate_agent=AsyncMock(side_effect=create)
    )
    runtimes.manager._harness_manager = harness
    runtimes.manager._post_start_agent = AsyncMock()
    runtimes.manager._build_harness_config = lambda _: SimpleNamespace(
        tools_disabled=frozenset(), mcp_server_configs={}
    )
    result = await asyncio.gather(
        runtimes.ensure(source.agent_id, 1), runtimes.ensure(source.agent_id, 1)
    )
    assert result == [fake_agent, fake_agent]
    assert harness.acreate_agent.await_count == 1


def test_backend_workspace_has_no_privileged_local_read_fallback(tmp_path: Path) -> None:
    sensitive = tmp_path / "secret.txt"
    sensitive.write_text("root-only-data")
    backend = UserWorkerBackend(MagicMock(), WorkerIdentity(1, 200001, tmp_path / "user", tmp_path))
    backend._rpc = MagicMock(return_value=[{"path": str(sensitive), "error": "permission_denied"}])  # type: ignore[method-assign]
    workspace = BackendWorkspace(backend, tmp_path)
    assert workspace.download_bytes(str(sensitive)) is None
    assert workspace._iter_materialize_candidates(str(sensitive)) == []


class _Settings:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value


@pytest.mark.asyncio
async def test_share_is_explicit_read_only_snapshot_and_revocable(tmp_path: Path) -> None:
    services = SimpleNamespace(
        settings_repo=_Settings(),
        paths=PathLayout(tmp_path),
        user_repo=SimpleNamespace(get=lambda uid: object()),
    )
    service = FileSharing(services)
    workspace = SimpleNamespace(adownload_bytes=AsyncMock(return_value=b"private file"))
    meta = await service.create(
        agent_id="COMMON", owner_id=1, path="report.txt", recipient_ids=[2], workspace=workspace
    )
    assert meta["read_only"] is True
    assert meta["snapshot"] is True
    assert service.download(meta["share_id"], 2) == ("report.txt", b"private file")
    with pytest.raises(OctopError):
        service.download(meta["share_id"], 3)
    with pytest.raises(OctopError):
        service.revoke(meta["share_id"], 2)
    service.revoke(meta["share_id"], 1)
    with pytest.raises(OctopError):
        service.download(meta["share_id"], 2)


@pytest.mark.parametrize(
    "path", ["../other", "/etc/passwd", ".octop/auth/token", ".env", ".", "a/../b", "bad\r\nname"]
)
def test_cannot_share_system_or_escape_paths(path: str) -> None:
    with pytest.raises(OctopError):
        share_path(path)
