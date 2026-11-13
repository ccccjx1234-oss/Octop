"""UID-scoped JSON worker. Started by absolute script path.

This module deliberately has no Octop import: booting a tool must not load
server configuration, credentials, plugins, or application startup hooks.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

# -I excludes the script directory. Load only this trusted sibling module.
sys.path.insert(0, str(Path(__file__).parent))
from codec import decode, encode  # noqa: E402

if sys.platform != "linux":
    raise RuntimeError("isolated workers require Linux")

OPERATIONS = frozenset(
    {
        "ls",
        "read",
        "write",
        "edit",
        "delete",
        "grep",
        "glob",
        "upload_files",
        "download_files",
        "mkdir_path",
        "delete_path",
        "move_path",
        "execute",
    }
)


def execute(command: str, *, timeout: int = 120) -> dict[str, Any]:
    """Keep shell stdout separate from the RPC response and reap its process group."""
    import tempfile

    # A file bounds parent memory even when a command produces a lot of output.
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", command],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        timed_out = False
        try:
            deadline = time.monotonic() + timeout
            # Keep the leader unreaped until group cleanup, preventing PID reuse
            # from redirecting killpg onto an unrelated group.
            while os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(0.02)
        finally:
            # Also terminate background descendants after a normally exiting shell.
            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        output.seek(0)
        data = output.read(1_048_577)
    text = data[:1_048_576].decode("utf-8", errors="replace")
    if timed_out:
        text += "\nCommand timed out."
    return {
        "output": text,
        "exit_code": 124 if timed_out else proc.returncode,
        "truncated": len(data) > 1_048_576,
    }


def dispatch(request: dict[str, Any]) -> Any:
    op = request["op"]
    if op not in OPERATIONS:
        raise ValueError("unsupported worker operation")
    if sys.platform != "linux" or os.geteuid() == 0 or os.getgroups():
        raise PermissionError("worker requires a Linux non-root identity without extra groups")
    # Defense in depth: setpriv must have cleared every effective capability.
    status = Path("/proc/self/status").read_text()
    if "NoNewPrivs:\t1" not in status:
        raise PermissionError("worker requires no_new_privs")
    for line in status.splitlines():
        if line.startswith(("CapEff:", "CapPrm:", "CapAmb:", "CapBnd:")) and int(
            line.split()[1], 16
        ):
            raise PermissionError("worker capabilities were not cleared")
    os.umask(0o077)
    args, kwargs = decode(request.get("args", [])), decode(request.get("kwargs", {}))
    if op == "execute":
        from octop_harness.backends.local_shell import rewrite_virtual_paths_in_command

        args[0] = rewrite_virtual_paths_in_command(args[0], Path.cwd(), workspace_dir=Path.cwd())
        return execute(*args, **kwargs)

    def workspace_key(value: Any) -> Any:
        # Shell scripts may return their real absolute output path. Normalize
        # only this worker's own prefix, after dropping privileges; never ask
        # the supervisor to resolve a user-controlled physical path.
        root = str(Path.cwd())
        if isinstance(value, str) and (value == root or value.startswith(root + "/")):
            return "/" + value[len(root) :].lstrip("/")
        return value

    if op in {"upload_files", "download_files"}:
        if op == "download_files":
            args[0] = [workspace_key(path) for path in args[0]]
        else:
            args[0] = [(workspace_key(path), data) for path, data in args[0]]
    elif op in {"glob", "grep"}:
        if op == "glob":
            args[0] = workspace_key(args[0])
        if len(args) > 1:
            args[1] = workspace_key(args[1])
    else:
        args[0] = workspace_key(args[0])
        if op == "move_path":
            args[1] = workspace_key(args[1])

    # Reuse the installed, version-matched backend rather than reimplementing
    # file pagination, grep, glob, edits, or result contracts here.
    from octop_harness.backends.local_shell import HarnessLocalShellBackend
    from octop_harness.backends.utils import delete_local_path, mkdir_local_path, move_local_path

    backend = HarnessLocalShellBackend(root_dir=Path.cwd(), virtual_mode=True, inherit_env=False)
    if op in {"mkdir_path", "delete_path", "move_path"}:
        paths = [Path(backend._resolve_path(str(value))) for value in args]
        if any(path == Path.cwd() for path in paths):
            raise PermissionError("cannot mutate workspace root")
        if op == "mkdir_path":
            return mkdir_local_path(paths[0])
        if op == "delete_path":
            return delete_local_path(paths[0])
        if paths[1].exists():
            raise FileExistsError(str(paths[1]))
        return move_local_path(paths[0], paths[1])
    return getattr(backend, op)(*args, **kwargs)


def prepare_workspace(workspace: Path) -> None:
    import resource

    if os.geteuid() == 0 or os.getgroups():
        raise PermissionError("worker must drop its identity before entering user directories")
    os.umask(0o077)
    workspace.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchdir(fd)
    finally:
        os.close(fd)
    Path(".tmp").mkdir(mode=0o700, exist_ok=True)
    # Legacy workspaces may retain tokens at .octop-auth even when newer
    # system files use .octop. Resolve this only in the user's process.
    legacy_auth = Path.cwd() / ".octop-auth"
    with suppress(PermissionError):
        if legacy_auth.is_dir():
            os.environ["OCTOP_AUTH_DIR"] = str(legacy_auth)
    for limit, value in (
        (resource.RLIMIT_NOFILE, 1024),
        (resource.RLIMIT_NPROC, 256),
        (resource.RLIMIT_FSIZE, 100 * 1024 * 1024),
        (resource.RLIMIT_CORE, 0),
    ):
        _, hard = resource.getrlimit(limit)
        value = min(value, hard) if hard != resource.RLIM_INFINITY else value
        resource.setrlimit(limit, (value, value))


def main() -> None:
    try:
        prepare_workspace(Path(sys.argv[1]))
        for line in sys.stdin:
            try:
                request = json.loads(line)
                prepare_workspace(Path(sys.argv[1]))
                result = {"result": encode(dispatch(request))}
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                result = {"error": type(exc).__name__, "message": str(exc)}
            sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
            sys.stdout.flush()
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        result = {"error": type(exc).__name__, "message": str(exc)}
        sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
