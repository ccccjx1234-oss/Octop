"""Cross-router request validation helpers (no FastAPI route definitions)."""

from __future__ import annotations

from typing import Any

from octop.infra.backend.sandbox import resolve_user_effective_root
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.resource_policy import (
    assert_backend_within_user_root,
    raise_if_backend_outside_user_root,
)
from octop.infra.utils.host_dirs import assert_backend_root_dirs_allowed


def assert_user_backend_root_dirs(
    user: Any,
    backend: Any,
    *,
    policy_repo: Any | None = None,
    paths: Any | None = None,
) -> None:
    """Reject local backend ``root_dir`` values that fail denylist or user policy.

    When *policy_repo* is provided, explicit ``local_shell`` / ``filesystem``
    roots must stay under the user's ``workspace_root_dir`` (or non-admin
    sandbox root when ``OCTOP_AGENT_SANDBOX_ISOLATION=1``). A missing root or
    the host filesystem root is left for create/update to snap onto that
    directory. All authenticated users may otherwise select host paths outside
    home (UI still defaults to home). Sensitive pseudo-fs mounts remain blocked.
    """
    if backend is None:
        return
    if policy_repo is not None and user is not None:
        if paths is not None:
            allowed = resolve_user_effective_root(
                paths=paths,
                policy_repo=policy_repo,
                user_id=int(user.id),
                is_admin=bool(getattr(user, "is_admin", False)),
            )
            if allowed:
                try:
                    assert_backend_within_user_root(backend, allowed, reject_host_root=False)
                except ValueError as exc:
                    raise OctopError(ErrorCode.WORKSPACE_ROOT_RESTRICTED, str(exc)) from exc
                return
        raise_if_backend_outside_user_root(policy_repo, int(user.id), backend)
        return
    try:
        assert_backend_root_dirs_allowed(backend, restrict_to_home=False)
    except ValueError as exc:
        raise OctopError(ErrorCode.WORKSPACE_OP_UNSUPPORTED, str(exc)) from exc


async def validate_chat_mcp_servers(
    server: Any,
    *,
    user_id: int,
    names: list[str] | None,
) -> list[str] | None:
    if names is None:
        return None
    if not names:
        return []
    from octop.infra.connectors.service import ConnectorService

    svc = ConnectorService(
        repo=server.services.repos.connector_repo,
        secret_repo=server.services.secret_repo,
        settings_repo=server.services.settings_repo,
        config=server.services.config,
    )
    try:
        return list(svc.validate_mcp_servers_for_user(user_id, names))
    except ValueError as exc:
        raise OctopError(ErrorCode.CONNECTOR_NOT_BOUND, str(exc)) from exc


async def validate_chat_skills(
    server: Any,
    *,
    agent_id: str,
    user: Any,
    names: list[str] | None,
) -> list[str] | None:
    """Validate per-turn skill filter for chat.

    ``None`` leaves the full skill set; ``[]`` disables all skills.
    """
    if names is None:
        return None
    assert server.app_runtime is not None
    allowed: set[str] = set()
    for summary in await server.app_runtime.agent_registry.list_skill_summaries(agent_id):
        if summary.get("enabled"):
            allowed.add(str(summary["name"]))
            slug = summary.get("slug")
            if slug:
                allowed.add(str(slug))
    bad = [n for n in names if n not in allowed]
    if bad:
        raise OctopError(
            ErrorCode.NOT_FOUND,
            f"skill(s) not found or disabled: {', '.join(bad)}",
        )
    return list(names)
