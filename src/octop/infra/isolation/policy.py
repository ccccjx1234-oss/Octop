"""Service-owned identity and directory policy for Linux workers."""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.isolation.repair import MetadataRepair, service_entry
from octop.infra.utils.paths import PathLayout

UID_BASE = 200_000
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerIdentity:
    user_id: int
    uid: int
    workspace: Path
    runtime: Path
    system_files_path: str = ".octop"


class IsolationPolicy:
    def __init__(self, paths: PathLayout):
        self.paths = paths
        self.root = paths.root.resolve()
        self.setpriv = ""
        self.mode = os.environ.get("OCTOP_USER_ISOLATION_STARTUP", "repair").strip().lower()
        if self.mode not in {"strict", "repair"}:
            raise RuntimeError("OCTOP_USER_ISOLATION_STARTUP must be strict or repair")
        self.state = "pending"
        self.error: str | None = None
        self.control_plane_safe = False
        self.repairs = MetadataRepair(self.root, enabled=self.mode == "repair")
        self._verified_homes: dict[int, tuple[int, int, int]] = {}

    def start(self) -> None:
        """Compatibility changes availability, never the worker's identity."""
        try:
            self.prepare()
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            self.state = "maintenance"
            self.error = str(exc)
            if self.mode == "strict":
                raise
            logger.error("user isolation unavailable; execution disabled: %s", exc)

    def require_ready(self) -> None:
        if self.state != "ready":
            raise OctopError(
                ErrorCode.WORKSPACE_OP_UNSUPPORTED,
                "user isolation is unavailable; inspect administrator isolation diagnostics",
                status=503,
                details={"isolation_state": self.state},
            )

    def validate_host(self) -> None:
        if sys.platform != "linux" or os.geteuid() != 0:
            raise RuntimeError("user isolation requires a Linux root supervisor")
        self.setpriv = shutil.which("setpriv", path="/usr/bin:/bin") or ""
        if not self.setpriv:
            raise RuntimeError("user isolation requires util-linux setpriv")
        status = Path("/proc/self/status").read_text()
        effective = next(
            int(line.split()[1], 16) for line in status.splitlines() if line.startswith("CapEff:")
        )
        # CHOWN, FOWNER, KILL, SETGID, SETUID, SETPCAP. Root alone is not
        # sufficient to provision and reap processes owned by another UID.
        required = sum(1 << bit for bit in (0, 3, 5, 6, 7, 8))
        if effective & required != required:
            raise RuntimeError("supervisor lacks required identity/ownership/process capabilities")
        # The supervisor must not be installed inside a user-writable workspace.
        for target in (Path(sys.executable), Path(__file__)):
            for parent in (target.resolve(), *target.resolve().parents):
                info = parent.stat()
                if info.st_uid != 0 or info.st_mode & 0o022:
                    raise RuntimeError(
                        f"worker code/runtime must be root-owned and immutable: {parent}"
                    )

    def prepare(self) -> None:
        if self.state == "ready":
            return
        self.state = "preparing"
        self.control_plane_safe = False
        if self.root in {Path("/"), Path("/etc"), Path("/var"), Path("/home"), Path("/root")}:
            raise RuntimeError("OCTOP_HOME must be a dedicated application data directory")
        if any(
            path.resolve().is_relative_to(self.root)
            for path in (Path(__file__), Path(sys.executable))
        ):
            raise RuntimeError(
                "worker code and interpreter must be outside the private data directory"
            )
        os.umask(0o077)
        if sys.platform != "linux" or os.geteuid() != 0:
            raise RuntimeError("user isolation requires a Linux root supervisor")
        self.root.mkdir(parents=True, exist_ok=True)
        original = self.paths.root.absolute()
        for entry in (original, *original.parents):
            if entry.is_symlink() and entry.lstat().st_uid != 0:
                raise RuntimeError(f"isolation data path alias must be owned by root: {entry}")
        for parent in dict.fromkeys((*self.root.parents, *original.parents)):
            info = parent.stat()
            if info.st_uid != 0 or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
                raise RuntimeError(
                    f"isolation data ancestor is writable by another identity: {parent}"
                )
        # Validate aliases before changing targets. Homes are checked lazily so
        # one invalid home cannot prevent other users from starting.
        for name in ("agents", "user-workspaces", "isolated-runtimes"):
            if (self.root / name).is_symlink():
                raise RuntimeError(f"isolation managed parent cannot be a symlink: {name}")
        entries = [service_entry(self.root, child) for child in self.root.iterdir()]
        self.repairs.apply(self.root, 0o711)
        for child in entries:
            if child.name in {"agents", "user-workspaces", "isolated-runtimes"}:
                continue
            self.repairs.apply(child, 0o700 if child.is_dir() else 0o600)
        for name in ("agents", "user-workspaces", "isolated-runtimes"):
            parent = self.root / name
            parent.mkdir(mode=0o700, exist_ok=True)
            self.repairs.apply(parent, 0o700 if name == "isolated-runtimes" else 0o711)
            if name == "agents":
                for child in parent.iterdir():
                    child = service_entry(self.root, child)
                    self.repairs.apply(child, 0o700 if child.is_dir() else 0o600)
            elif name == "user-workspaces":
                for child in parent.iterdir():
                    if child.is_symlink():
                        continue  # Refuse this home on use; never chmod its target.
                    if not child.name.isdecimal() or not child.is_dir():
                        raise RuntimeError(f"unexpected entry in user workspaces: {child.name}")
                    owner = child.stat().st_uid
                    if owner not in {0, UID_BASE + int(child.name)}:
                        raise RuntimeError(f"worker home belongs to another identity: {child.name}")
                    self.repairs.apply(child, 0o700, owner=owner)
        self.control_plane_safe = True
        self.validate_host()
        self._probe_worker_access()
        self.state = "ready"
        self.error = None

    def _probe_worker_access(self) -> None:
        # Probe a real UID: valid ACL-based traversal need not grant other+x.
        uid = self._uid(1)
        script = (
            "import os,sys; "
            "assert os.geteuid()==int(sys.argv[2]); "
            "assert os.stat(sys.argv[1]); "
            "assert os.access(sys.argv[1],os.X_OK); "
            "assert os.access(os.path.join(sys.argv[1],'user-workspaces'),os.X_OK); "
            "assert not os.access(sys.argv[1],os.R_OK); "
            "assert not os.access(sys.argv[1],os.W_OK); "
            "assert not os.access(sys.argv[3],os.R_OK); "
            "assert not os.access(sys.argv[3],os.X_OK)"
        )
        result = subprocess.run(
            self._command(
                uid,
                [
                    sys.executable,
                    "-I",
                    "-c",
                    script,
                    str(self.root),
                    str(uid),
                    str(self.root / "isolated-runtimes"),
                ],
            ),
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            cwd="/",
            capture_output=True,
            timeout=15,
            check=False,
        )
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()[:2000]
            raise RuntimeError(
                "low-privilege worker probe failed; verify data ancestor traversal, "
                f"interpreter permissions and setpriv support: {detail}"
            )

    @staticmethod
    def _uid(user_id: int) -> int:
        import grp
        import pwd

        uid = UID_BASE + user_id
        for lookup in (pwd.getpwuid, grp.getgrgid):
            try:
                lookup(uid)
            except KeyError:
                pass
            else:
                raise RuntimeError(
                    f"reserved worker UID/GID {uid} conflicts with a system account/group"
                )
        return uid

    def _prepare_home(self, user_id: int, uid: int) -> Path:
        home = self.root / "user-workspaces" / str(user_id)
        if home.is_symlink():
            raise PermissionError("worker home cannot be a symlink")
        home.mkdir(mode=0o700, exist_ok=True)
        if home.stat().st_uid not in {0, uid}:
            raise PermissionError("worker home belongs to another identity")
        # Never traverse or change user-controlled descendants as root.
        self.repairs.apply(home, 0o700, owner=uid, provision_owner=True)
        info = home.stat()
        fingerprint = (info.st_dev, info.st_ino, info.st_ctime_ns)
        if self._verified_homes.get(uid) != fingerprint:
            result = subprocess.run(
                self._command(
                    uid,
                    [
                        sys.executable,
                        "-I",
                        "-c",
                        "import os,sys; assert os.access(sys.argv[1],os.R_OK|os.W_OK|os.X_OK)",
                        str(home),
                    ],
                ),
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                cwd="/",
                capture_output=True,
                timeout=15,
                check=False,
            )
            if result.returncode:
                raise PermissionError("worker UID cannot access its private home")
            self._verified_homes[uid] = fingerprint
        return home

    def identity(
        self, user_id: int, runtime_key: str, *, system_files_path: str = ".octop"
    ) -> WorkerIdentity:
        self.require_ready()
        if isinstance(user_id, bool) or user_id <= 0 or user_id > 1_000_000_000:
            raise ValueError("invalid platform user id")
        if not runtime_key or not runtime_key.replace("-", "").isalnum():
            raise ValueError("invalid runtime key")
        if system_files_path not in {"", ".octop"}:
            raise ValueError("unsupported worker system files prefix")
        try:
            uid = self._uid(user_id)
            home = self._prepare_home(user_id, uid)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            logger.warning("isolated workspace unavailable for user %s: %s", user_id, exc)
            raise OctopError(
                ErrorCode.WORKSPACE_OP_UNSUPPORTED,
                "this user's isolated workspace is unavailable",
                status=503,
                details={"isolation_state": "user_unavailable"},
            ) from exc
        workspace = home / runtime_key
        # Children of this directory are user-controlled. Never chown/chmod
        # them as root: create through the low-privilege provisioning process.
        runtime = self.root / "isolated-runtimes" / runtime_key
        runtime.mkdir(mode=0o700, exist_ok=True)
        self.repairs.apply(runtime, 0o700)
        return WorkerIdentity(user_id, uid, workspace, runtime, system_files_path)

    def command(self, identity: WorkerIdentity, argv: list[str]) -> list[str]:
        self.require_ready()
        return self._command(identity.uid, argv)

    def _command(self, uid: int, argv: list[str]) -> list[str]:
        return [
            self.setpriv,
            f"--reuid={uid}",
            f"--regid={uid}",
            "--clear-groups",
            "--inh-caps=-all",
            "--ambient-caps=-all",
            "--bounding-set=-all",
            "--no-new-privs",
            "--",
            *argv,
        ]

    def environment(self, identity: WorkerIdentity) -> dict[str, str]:
        # Do not inherit DB passwords, JWT secrets, admin env, PYTHONPATH, or
        # LD_PRELOAD from the privileged supervisor.
        system = identity.workspace / identity.system_files_path
        auth = system / "auth" if identity.system_files_path else identity.workspace / ".octop-auth"
        return {
            "PATH": f"{Path(sys.executable).parent}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(identity.workspace),
            "USER": f"octop-user-{identity.user_id}",
            "LOGNAME": f"octop-user-{identity.user_id}",
            "TMPDIR": str(identity.workspace / ".tmp"),
            "LANG": "C.UTF-8",
            "OCTOP_AUTH_DIR": str(auth),
            "OCTOP_SKILLS_DIR": str(system / "skills"),
        }
