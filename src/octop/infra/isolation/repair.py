"""Bounded metadata repairs for an exclusively owned Octop data directory."""

from __future__ import annotations

import base64
import errno
import json
import os
import stat
from pathlib import Path

_DEFAULT_ACL = "system.posix_acl_default"
_NO_ACL = {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP}
_JOURNAL = ".isolation-repairs.jsonl"


def default_acl(fd: int) -> bytes:
    try:
        return os.getxattr(f"/proc/self/fd/{fd}", _DEFAULT_ACL)
    except OSError as exc:
        if exc.errno not in _NO_ACL:
            raise
        return b""


def open_entry(path: Path) -> int:
    # O_PATH needs no read access to another UID's 700 home. Keep the inode
    # pinned while changing metadata via procfs, without requiring DAC_OVERRIDE.
    fd = os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
    info = os.fstat(fd)
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or (
        stat.S_ISREG(info.st_mode) and info.st_nlink != 1
    ):
        os.close(fd)
        raise RuntimeError(
            f"isolation metadata target must be a directory or file with one link: {path}"
        )
    return fd


class MetadataRepair:
    """Journal only owner/mode/default ACL; never copy data or recurse into files."""

    def __init__(self, root: Path, *, enabled: bool):
        self.root = root
        self.enabled = enabled
        self.count = 0

    def apply(
        self, path: Path, mode: int, *, owner: int = 0, provision_owner: bool = False
    ) -> None:
        fd = open_entry(path)
        try:
            info = os.fstat(fd)
            acl = default_acl(fd) if stat.S_ISDIR(info.st_mode) else b""
            owner_changed = info.st_uid != owner
            if not self.enabled:
                if owner_changed and not provision_owner:
                    raise RuntimeError(f"isolation directory belongs to another identity: {path}")
                if acl:
                    raise RuntimeError(f"isolation directory default ACL must be removed: {path}")
            changed = owner_changed or stat.S_IMODE(info.st_mode) != mode or bool(acl)
            if not changed:
                return
            if changed and self.enabled:
                self._record(path, info, acl)
            if owner_changed:
                os.chown(f"/proc/self/fd/{fd}", owner, owner)
            if acl:
                os.removexattr(f"/proc/self/fd/{fd}", _DEFAULT_ACL)
            # chmod also masks any inherited named access ACL entries.
            os.chmod(f"/proc/self/fd/{fd}", mode)
            after = os.fstat(fd)
            if (
                after.st_uid != owner
                or stat.S_IMODE(after.st_mode) != mode
                or (stat.S_ISDIR(after.st_mode) and default_acl(fd))
            ):
                raise RuntimeError(f"filesystem did not retain isolation metadata: {path}")
        finally:
            os.close(fd)

    def _record(self, path: Path, info: os.stat_result, acl: bytes) -> None:
        # The journal itself must not be an alias for an unrelated host file.
        journal = self.root / _JOURNAL
        fd = os.open(
            journal,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            0o600,
        )
        try:
            journal_info = os.fstat(fd)
            if not stat.S_ISREG(journal_info.st_mode) or journal_info.st_nlink != 1:
                raise RuntimeError("isolation repair journal must be a regular file with one link")
            os.fchown(fd, 0, 0)
            os.fchmod(fd, 0o600)
            record = {
                "path": str(path.relative_to(self.root)),
                "uid": info.st_uid,
                "gid": info.st_gid,
                "mode": oct(stat.S_IMODE(info.st_mode)),
                "default_acl": base64.b64encode(acl).decode("ascii"),
            }
            data = (json.dumps(record, ensure_ascii=True) + "\n").encode()
            with os.fdopen(fd, "ab", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(fd)
            self.count += 1
        finally:
            os.close(fd)


def service_entry(root: Path, path: Path) -> Path:
    """Allow an admin-owned alias only to private, root-owned service data."""
    if not path.is_symlink():
        return path
    if path.lstat().st_uid != 0:
        raise RuntimeError(f"isolation symlink must be owned by root: {path}")
    target = path.resolve(strict=True)
    if target == root or not target.is_relative_to(root):
        raise RuntimeError(f"isolation symlink target must remain inside OCTOP_HOME: {path}")
    relative = target.relative_to(root)
    if relative.parts[0] in {"user-workspaces", "agents", "isolated-runtimes", _JOURNAL}:
        raise RuntimeError(f"isolation symlink cannot alias a workspace or runtime: {path}")
    for entry in (target, *target.parents):
        if entry == root:
            break
        info = entry.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError(f"isolation symlink target must be root-owned and immutable: {path}")
    return target
