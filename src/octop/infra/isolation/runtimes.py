"""Share expert definitions, never their user runtime, memory, or files."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any, cast

from octop.infra.db.repos.agents import AgentRow
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.isolation.backend import UserWorkerBackend
from octop.infra.isolation.context import actor_id
from octop.infra.isolation.policy import IsolationPolicy
from octop.infra.users.groups import expert_visible_to

if TYPE_CHECKING:
    from octop_harness.config import HarnessAgentConfig


def runtime_key(agent_id: str, user_id: int) -> str:
    digest = hashlib.sha256(agent_id.encode()).hexdigest()[:24]
    return f"u{user_id}a{digest}"


class UserRuntimes:
    def __init__(self, manager: Any, policy: IsolationPolicy):
        self.manager = manager
        self.policy = policy
        self.rows: dict[str, AgentRow] = {}
        self.sources: dict[str, str] = {}
        self.fingerprints: dict[str, str] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.backends: dict[str, UserWorkerBackend] = {}
        self._backend_lock = threading.Lock()

    def source_row(self, agent_id: str) -> AgentRow:
        row = self.manager._repos.agent_repo.get(self.sources.get(agent_id, agent_id))
        if row is None:
            raise OctopError(ErrorCode.AGENT_NOT_FOUND, "expert does not exist")
        return cast(AgentRow, row)

    def row(self, agent_id: str, user_id: int | None = None) -> AgentRow:
        source = self.source_row(agent_id)
        user_id = user_id if user_id is not None else actor_id()
        # A shared expert has no implicit caller and never borrows its creator.
        if user_id is None and not source.is_shared:
            user_id = source.user_id
        if user_id is None:
            raise OctopError(ErrorCode.FORBIDDEN, "shared expert requires an authenticated actor")
        user = self.manager._repos.user_repo.get(user_id)
        if user is None or getattr(user, "disabled", False) or not source.enabled:
            raise OctopError(ErrorCode.FORBIDDEN, "expert or user is unavailable")
        if not expert_visible_to(
            self.manager._repos, source, user_id=user_id, is_admin=str(user.role) == "admin"
        ):
            raise OctopError(ErrorCode.FORBIDDEN, "expert is not visible to this user")
        # Include the immutable DB primary key so a deleted short public id
        # cannot attach a new definition to a previous definition's state.
        key = runtime_key(f"{source.id}:{source.agent_id}", user_id)
        identity = self.policy.identity(user_id, key)
        cfg = self.manager._agent_config_dict(source)
        settings = getattr(self.manager._repos, "settings_repo", None)
        overrides = (
            settings.get("isolation.runtime_config." + key) if settings is not None else None
        )
        if overrides:
            cfg.update(json.loads(overrides))
        # User-chosen host mounts and shared memory paths cannot become a
        # privileged read route. Runtime state stays in a service-only directory.
        cfg["workspace_dir"] = str(identity.runtime)
        cfg["backend"] = {
            "type": "filesystem",
            "root_dir": str(identity.runtime),
            "virtual_mode": False,
        }
        # Visibility changes must not relocate an existing user's system files.
        prefix = cfg.get("system_files_path", "")
        if prefix not in {"", ".octop"}:
            raise RuntimeError("unsupported system files prefix for isolated runtime")
        cfg["system_files_path"] = prefix
        memory = cfg.get("memory")
        memory_backend: dict[str, Any] = {"type": "sqlite"}
        if self.manager.octop_config.database.is_postgresql:
            from octop.infra.isolation.postgres import memory_backend as postgres_memory_backend

            memory_backend = postgres_memory_backend(
                self.manager.octop_config.database.postgresql_conninfo(), key
            )
        cfg["memory"] = {
            **(memory if isinstance(memory, dict) else {}),
            "backend": memory_backend,
        }
        cfg.pop("skills", None)
        row = replace(
            source, agent_id=key, user_id=user_id, is_shared=0, config_json=json.dumps(cfg)
        )
        self.rows[key] = row
        self.sources[key] = source.agent_id
        return row

    def backend(self, row: AgentRow) -> UserWorkerBackend:
        if row.user_id is None or row.agent_id not in self.rows:
            raise OctopError(ErrorCode.FORBIDDEN, "unscoped backend is forbidden")
        with self._backend_lock:
            if row.agent_id not in self.backends:
                backend_class = UserWorkerBackend
                if self.manager.octop_config.database.is_postgresql:
                    from octop.infra.isolation.postgres import PostgresWorkerBackend

                    backend_class = PostgresWorkerBackend
                self.backends[row.agent_id] = backend_class(
                    self.policy,
                    self.policy.identity(
                        row.user_id,
                        row.agent_id,
                        system_files_path=json.loads(row.config_json or "{}").get(
                            "system_files_path", ""
                        ),
                    ),
                )
            return self.backends[row.agent_id]

    async def remove_source(self, agent_id: str) -> None:
        if self.manager.is_agent_active(agent_id):
            raise OctopError(ErrorCode.AGENT_BUSY, "expert still has active user turns")
        for key, source in list(self.sources.items()):
            if source == agent_id:
                async with self.locks.setdefault(key, asyncio.Lock()):
                    backend = self.backends.get(key)
                    if backend is not None:
                        await asyncio.to_thread(backend.close)
                    await self.manager._harness_manager.aremove_agent(key)
                    self.fingerprints.pop(key, None)

    def configure(self, cfg: HarnessAgentConfig, row: AgentRow) -> HarnessAgentConfig:
        from octop.infra.isolation.mcp import scope_mcp_spec
        from octop.infra.isolation.tool_guard import IsolationToolGuard

        key = row.agent_id
        backend = self.backend(row)
        cfg.tools = None
        cfg.mcp_server_configs = {
            name: scope_mcp_spec(spec, backend) for name, spec in cfg.mcp_server_configs.items()
        }
        cfg.acp_runners = {}
        cfg.acp_delegate_enabled = False
        cfg.skills_dir = None
        cfg.memory_namespace = self.namespace(row)
        prefix = json.loads(row.config_json or "{}").get("system_files_path", "")
        cfg.memory_backend = {
            "type": "sqlite",
            "db_path": str(self.policy.root / "isolated-runtimes" / key / prefix / "memory.sqlite"),
        }
        if self.manager.octop_config.database.is_postgresql:
            from octop.infra.isolation.postgres import PostgresWorkerBackend, memory_backend

            cfg.memory_backend = memory_backend(
                self.manager.octop_config.database.postgresql_conninfo(), key
            )
            assert isinstance(backend, PostgresWorkerBackend)
            # Memory can be disabled without moving conversation checkpoints
            # back to SQLite or changing their history when it is enabled later.
            cfg.checkpointer = backend.checkpointer(cfg.memory_backend["dsn"])
        cfg.team_enabled = False
        cfg.subagents_auto_load = False
        cfg.media_generation = None
        cfg.web_search_tools = False
        cfg.tools_disabled = frozenset(
            set(cfg.tools_disabled)
            | {
                "browser_use",
                "desktop_screenshot",
                "task",
                "ask_agent",
                "agent_list",
                "read_env_file",
                "write_env_file",
                "web_fetch",
            }
        )
        cfg.middleware = [
            IsolationToolGuard(
                lambda: getattr(
                    self.manager._harness_manager.get_agent(key).agent,
                    "_mcp_tool_name_set",
                    frozenset(),
                )
            )
        ]
        return cfg

    def namespace(self, row: AgentRow) -> str:
        source = self.source_row(row.agent_id)
        identity = source.agent_id if source.user_id == row.user_id else row.agent_id
        return f"agent_{identity}"

    async def ensure(self, agent_id: str, user_id: int | None = None) -> Any:
        row = await asyncio.to_thread(self.row, agent_id, user_id)
        key = row.agent_id
        definition = asdict(row)
        for transient in ("last_state", "last_error", "updated_at"):
            definition.pop(transient, None)
        fingerprint = json.dumps(definition, sort_keys=True)
        async with self.locks.setdefault(key, asyncio.Lock()):
            harness = self.manager._harness_manager
            if harness is None or harness.shared_factory is None:
                raise self.manager._unavailable_error(agent_id)
            try:
                entry = harness.get_agent(key)
            except KeyError:
                entry = None
            if entry is not None and self.fingerprints.get(key) == fingerprint:
                return entry.agent
            if entry is not None:
                if self.manager.is_agent_active(self.source_row(agent_id).agent_id):
                    return entry.agent
                backend = self.backends.get(key)
                if backend is not None:
                    await asyncio.to_thread(backend.close)
                await harness.aremove_agent(key)
            from octop.infra.isolation.migration import migrate_owner_workspace

            await asyncio.to_thread(migrate_owner_workspace, self, row)
            cfg = await asyncio.to_thread(self.manager._build_harness_config, row)
            entry = await harness.acreate_agent(cfg, agent_id=key, init_workspace=True)
            try:
                template_marker = self.policy.root / "isolated-runtimes" / key / ".template-seeded"
                if row.template_name and not template_marker.exists():
                    await self.manager._seed_expert_template(row, row.template_name)
                    template_marker.write_text(row.template_name)
                await self.manager._post_start_agent(row, entry.agent, cfg)
            except BaseException:
                await harness.aremove_agent(key)
                raise
            self.fingerprints[key] = fingerprint
            return entry.agent
