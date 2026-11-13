"""Copy an owner's legacy workspace once; never redistribute shared history."""

from __future__ import annotations

import errno
import os
import shlex
import stat
from pathlib import Path
from typing import Any


def migrate_owner_workspace(runtimes: Any, row: Any) -> None:
    if os.name != "posix":
        raise RuntimeError("workspace migration requires POSIX")
    source = runtimes.source_row(row.agent_id)
    backend = runtimes.backend(row)
    marker = backend.identity.runtime / ".owner-migration-complete"
    if marker.exists():
        return
    if source.is_shared or source.user_id != row.user_id:
        marker.write_text("private instance; shared history was not imported")
        return
    from octop.infra.agents.workspace.dir import workspace_dir_from_config

    cfg = runtimes.manager._agent_config_dict(source)
    original = workspace_dir_from_config(
        cfg, paths=runtimes.policy.paths, agent_id=source.agent_id
    ).resolve()
    if not original.exists():
        marker.write_text("no legacy workspace")
        return
    # Host-root or external mounts require an explicit import. Never copy a
    # creator-selected host mount into a private runtime as a side effect.
    if not original.is_relative_to(runtimes.policy.root / "agents"):
        raise RuntimeError("legacy external workspace requires an explicit import before isolation")
    pending: list[tuple[str, bytes]] = []
    executable_paths: list[str] = []
    pending_size = 0

    def flush() -> None:
        nonlocal pending_size
        replies = backend.upload_files(pending)
        if len(replies) != len(pending) or any(reply.error for reply in replies):
            raise OSError("legacy workspace import failed")
        pending.clear()
        pending_size = 0

    for parent, _dirs, files, dir_fd in os.fwalk(original, follow_symlinks=False):
        for name in files:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
            except OSError as exc:
                # A symlink is neither opened by the supervisor nor flattened.
                if exc.errno == errno.ELOOP:
                    continue
                raise
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    continue
                if info.st_size > 100 * 1024 * 1024:
                    raise RuntimeError("legacy file exceeds 100 MiB; explicit import required")
                data = stream.read(100 * 1024 * 1024 + 1)
            if len(data) > 100 * 1024 * 1024:
                raise RuntimeError("legacy file grew beyond migration limit")
            relative = (Path(parent) / name).relative_to(original)
            # Runtime persistence is service-owned. No user-controlled symlink
            # is ever reproduced in this directory.
            state = backend.identity.runtime / relative
            state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            state.write_bytes(data)
            pending.append(("/" + relative.as_posix(), data))
            if info.st_mode & stat.S_IXUSR:
                executable_paths.append(relative.as_posix())
            pending_size += len(data)
            if pending_size >= 10 * 1024 * 1024:
                flush()
    if pending:
        flush()
    for start in range(0, len(executable_paths), 100):
        names = " ".join(shlex.quote(name) for name in executable_paths[start : start + 100])
        if backend.execute("chmod u+x -- " + names).exit_code:
            raise OSError("cannot restore owner execute permissions")
    marker.write_text("owner workspace copied; original retained")
