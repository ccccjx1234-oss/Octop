"""Per-agent bubblewrap sandbox root confinement for non-admin users."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.resource_policy import (
    POLICY_WORKSPACE_ROOT_DIR,
    assert_backend_within_user_root,
    clamp_backend_to_workspace_root,
    effective_workspace_root_dir,
)
from octop.infra.utils.host_dirs import assert_safe_host_path, is_host_filesystem_root
from octop.infra.utils.paths import PathLayout

ENV_AGENT_SANDBOX_ISOLATION = "OCTOP_AGENT_SANDBOX_ISOLATION"
SANDBOXES_DIRNAME = "sandboxes"


def sandbox_isolation_enabled() -> bool:
    """True when non-admin experts must be confined under per-agent sandbox roots."""
    raw = os.environ.get(ENV_AGENT_SANDBOX_ISOLATION, "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def user_sandbox_root(paths: PathLayout, user_id: int, *, ensure: bool = True) -> Path:
    """Return the user-level sandbox parent directory ``{OCTOP_HOME}/sandboxes/u{id}``."""
    target = (paths.root / SANDBOXES_DIRNAME / f"u{int(user_id)}").expanduser().resolve()
    if ensure:
        target.mkdir(parents=True, exist_ok=True)
    return target


def agent_sandbox_root(
    paths: PathLayout,
    user_id: int,
    agent_id: str,
    *,
    ensure: bool = True,
) -> Path:
    """Return the per-agent sandbox root ``{OCTOP_HOME}/sandboxes/u{id}/{agent_id}``."""
    parent = user_sandbox_root(paths, user_id, ensure=ensure)
    target = (parent / agent_id).resolve()
    if ensure:
        target.mkdir(parents=True, exist_ok=True)
    return target


def is_user_sandbox_dir(path: Any, user_root: Path) -> bool:
    """True when *path* points to *user_root* itself (the UI's create-time default)."""
    if not isinstance(path, str) or not path.strip():
        return False
    try:
        return Path(path).expanduser().resolve() == user_root.resolve()
    except OSError:
        return False


def _replace_user_root_with_agent_root(
    spec: Mapping[str, Any],
    *,
    user_root: Path,
    agent_root: Path,
) -> dict[str, Any]:
    """Upgrade a create-time default ``user_root`` backend to ``agent_root`` for per-agent isolation."""
    kind = str(spec.get("type") or "").strip().lower()
    if kind in {"local_shell", "filesystem"}:
        root = spec.get("root_dir")
        if is_host_filesystem_root(root) or is_user_sandbox_dir(root, user_root):
            return {**spec, "root_dir": str(agent_root)}
        return dict(spec)
    if kind == "composite":
        out = dict(spec)
        default = out.get("default")
        if isinstance(default, Mapping):
            out["default"] = _replace_user_root_with_agent_root(
                default, user_root=user_root, agent_root=agent_root
            )
        routes = out.get("routes")
        if isinstance(routes, Mapping):
            out["routes"] = {
                str(prefix): (
                    _replace_user_root_with_agent_root(
                        route, user_root=user_root, agent_root=agent_root
                    )
                    if isinstance(route, Mapping)
                    else route
                )
                for prefix, route in routes.items()
            }
        return out
    return dict(spec)


def resolve_user_effective_root(
    *,
    paths: PathLayout,
    policy_repo: Any,
    user_id: int,
    is_admin: bool,
    ensure: bool = True,
) -> str | None:
    """Return explicit ``workspace_root_dir`` policy or non-admin sandbox root."""
    explicit = effective_workspace_root_dir(
        policy_repo.get(user_id, POLICY_WORKSPACE_ROOT_DIR)
    )
    if explicit:
        return explicit
    if sandbox_isolation_enabled() and not is_admin:
        return str(user_sandbox_root(paths, user_id, ensure=ensure))
    return None


def apply_sandbox_on_create(
    *,
    paths: PathLayout,
    policy_repo: Any,
    user_id: int,
    is_admin: bool,
    agent_id: str,
    config: dict[str, Any],
) -> None:
    """Enforce per-agent sandbox ``root_dir`` when creating an expert for a non-admin user."""
    if not sandbox_isolation_enabled() or is_admin:
        return
    if effective_workspace_root_dir(policy_repo.get(user_id, POLICY_WORKSPACE_ROOT_DIR)):
        return

    user_root = user_sandbox_root(paths, user_id, ensure=True)
    agent_root = agent_sandbox_root(paths, user_id, agent_id, ensure=True)
    backend = config.get("backend")

    if isinstance(backend, Mapping) and backend:
        backend = _replace_user_root_with_agent_root(
            backend, user_root=user_root, agent_root=agent_root
        )
    try:
        config["backend"] = clamp_backend_to_workspace_root(backend, str(agent_root))
    except ValueError:
        # Custom subdirectory picked inside user_root (not under agent_root) is allowed.
        try:
            assert_backend_within_user_root(backend, str(user_root), reject_host_root=True)
            config["backend"] = dict(backend) if isinstance(backend, Mapping) else backend
        except ValueError as exc:
            raise OctopError(ErrorCode.WORKSPACE_ROOT_RESTRICTED, str(exc)) from exc

    raw_ws = config.get("workspace_dir")
    if isinstance(raw_ws, str) and raw_ws.strip():
        from octop.infra.agents.workspace.dir import resolve_workspace_host_path  # noqa: PLC0415

        try:
            host = resolve_workspace_host_path(raw_ws, config)
            assert_safe_host_path(os.fspath(host), restrict_to_root=str(user_root))
        except ValueError as exc:
            raise OctopError(ErrorCode.WORKSPACE_ROOT_RESTRICTED, str(exc)) from exc


def validate_sandbox_on_update(
    *,
    paths: PathLayout,
    policy_repo: Any,
    user_id: int,
    is_admin: bool,
    existing_cfg: Mapping[str, Any],
    incoming_cfg: Mapping[str, Any],
) -> None:
    """Prevent non-admin users from moving an expert outside their sandbox root."""
    if not sandbox_isolation_enabled() or is_admin:
        return
    if effective_workspace_root_dir(policy_repo.get(user_id, POLICY_WORKSPACE_ROOT_DIR)):
        return

    user_root = str(user_sandbox_root(paths, user_id, ensure=True))
    existing_backend = existing_cfg.get("backend")
    incoming_backend = incoming_cfg.get("backend")

    from octop.infra.users.resource_policy import persisted_workspace_within_root  # noqa: PLC0415

    already_sandboxed = persisted_workspace_within_root(existing_cfg, user_root)
    if already_sandboxed or incoming_backend != existing_backend:
        try:
            assert_backend_within_user_root(
                incoming_backend,
                user_root,
                reject_host_root=True,
            )
        except ValueError as exc:
            raise OctopError(ErrorCode.WORKSPACE_ROOT_RESTRICTED, str(exc)) from exc
