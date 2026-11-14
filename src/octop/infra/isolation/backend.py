"""Backend protocol proxy: every file operation crosses the UID boundary."""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import suppress
from pathlib import Path
from typing import Any

from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    SandboxBackendProtocol,
    WriteResult,
)

from octop.infra.isolation.codec import decode, encode
from octop.infra.isolation.policy import IsolationPolicy, WorkerIdentity

_stderr_logger = logging.getLogger(__name__)


class UserWorkerBackend(SandboxBackendProtocol):
    # Deliberately expose no cwd/root_dir/_resolve_path. BackendWorkspace must
    # never fall back to a privileged local read or mutation for this backend.
    sandbox_fs = True

    def __init__(self, policy: IsolationPolicy, identity: WorkerIdentity):
        self.policy = policy
        self.identity = identity
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._socket: socket.socket | None = None
        self._stderr_tail: deque[str] = deque(maxlen=30)

    @property
    def id(self) -> str:
        return f"user-{self.identity.user_id}-{self.identity.runtime.name}"

    def _rpc(self, op: str, *args: Any, **kwargs: Any) -> Any:
        payload = json.dumps({"op": op, "args": encode(args), "kwargs": encode(kwargs)})
        if len(payload) > 150 * 1024 * 1024:
            raise ValueError("worker request exceeds transport limit")
        with self._lock:
            try:
                if self._process is None or self._process.poll() is not None:
                    self._close_locked()
                    self._start()
                assert self._socket is not None
                deadline = time.monotonic() + int(kwargs.get("timeout") or 120) + 30
                self._socket.settimeout(max(0.001, deadline - time.monotonic()))
                self._socket.sendall(payload.encode() + b"\n")
                chunks: list[bytes] = []
                size = 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("isolated worker exceeded its deadline")
                    self._socket.settimeout(remaining)
                    chunk = self._socket.recv(65536)
                    if not chunk:
                        tail = " | ".join(list(self._stderr_tail)[-5:])
                        msg = (
                            f"isolated worker closed its transport: {tail}"
                            if tail
                            else "isolated worker closed its transport"
                        )
                        raise PermissionError(msg)
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > 150 * 1024 * 1024:
                        raise OSError("worker response exceeds transport limit")
                    if chunk.endswith(b"\n"):
                        break
                reply = json.loads(b"".join(chunks))
            except BaseException:
                self._close_locked()
                raise
        if "error" in reply:
            error = {
                "PermissionError": PermissionError,
                "FileNotFoundError": FileNotFoundError,
                "FileExistsError": FileExistsError,
                "ValueError": ValueError,
            }.get(reply["error"], OSError)
            raise error(reply.get("message", "worker operation failed"))
        return decode(reply["result"])

    def _start(self) -> None:
        worker = str(Path(__file__).with_name("worker.py"))
        argv = self.policy.command(
            self.identity, [sys.executable, "-I", worker, str(self.identity.workspace)]
        )
        parent, child = socket.socketpair()
        try:
            self._stderr_tail.clear()
            self._process = subprocess.Popen(
                argv,
                stdin=child.fileno(),
                stdout=child.fileno(),
                stderr=subprocess.PIPE,
                env=self.policy.environment(self.identity),
                cwd="/",
                close_fds=True,
                start_new_session=True,
            )
            if self._process.stderr is not None:
                uid = self.identity.user_id
                tail = self._stderr_tail
                pipe = self._process.stderr

                def _drain_stderr() -> None:
                    try:
                        for raw_line in pipe:
                            text = raw_line.decode("utf-8", errors="replace").rstrip()
                            if text:
                                tail.append(text)
                                _stderr_logger.warning(
                                    "isolated_worker stderr uid=%s: %s", uid, text
                                )
                    except Exception:  # noqa: BLE001
                        pass
                    finally:
                        with suppress(OSError):
                            pipe.close()

                threading.Thread(
                    target=_drain_stderr,
                    name=f"worker-stderr-{uid}",
                    daemon=True,
                ).start()
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        self._socket = parent

    def _close_locked(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        if self._process is not None:
            if self._process.poll() is None:
                with suppress(ProcessLookupError):
                    if sys.platform == "linux":
                        os.killpg(self._process.pid, signal.SIGKILL)
                    else:
                        self._process.kill()
            self._process.wait(timeout=5)
            self._process = None

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def ls(self, path: str) -> LsResult:
        return LsResult(**self._rpc("ls", path))

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return ReadResult(**self._rpc("read", file_path, offset, limit))

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(**self._rpc("write", file_path, content))

    def edit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        return EditResult(**self._rpc("edit", file_path, old_string, new_string, replace_all))

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(**self._rpc("delete", file_path))

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        return GrepResult(**self._rpc("grep", pattern, path, glob, max_count=max_count))

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        return GlobResult(**self._rpc("glob", pattern, path))

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [FileUploadResponse(**item) for item in self._rpc("upload_files", files)]

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return [FileDownloadResponse(**item) for item in self._rpc("download_files", paths)]

    def mkdir_path(self, path: str) -> None:
        self._rpc("mkdir_path", path)

    def delete_path(self, path: str) -> None:
        self._rpc("delete_path", path)

    def move_path(self, src: str, dest: str) -> None:
        self._rpc("move_path", src, dest)

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        if timeout is not None and not 1 <= timeout <= 600:
            raise ValueError("execute timeout must be between 1 and 600 seconds")
        return ExecuteResponse(**self._rpc("execute", command, timeout=timeout or 120))
