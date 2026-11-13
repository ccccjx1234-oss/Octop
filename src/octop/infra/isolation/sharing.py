"""Explicit, revocable read-only file snapshots for named platform users."""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import PurePosixPath
from typing import Any

from octop.infra.errors import ErrorCode, OctopError

_PREFIX = "isolation.file_share."
MAX_SHARE_BYTES = 100 * 1024 * 1024


def share_path(path: str) -> str:
    if any(ord(char) < 32 for char in path):
        raise OctopError(ErrorCode.SLASH_BAD_ARGS, "file path contains control characters")
    rel = PurePosixPath(path.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or str(rel) == ".":
        raise OctopError(ErrorCode.SLASH_BAD_ARGS, "share requires a workspace-relative file")
    # Tokens, skill config, and runtime metadata are not ordinary shareable files.
    if any(part.startswith(".") for part in rel.parts):
        raise OctopError(ErrorCode.FORBIDDEN, "system and hidden files cannot be shared")
    return str(rel)


class FileSharing:
    def __init__(self, services: Any):
        self.services = services
        self.settings = services.settings_repo
        self.root = services.paths.root / "file-shares"

    async def create(
        self, *, agent_id: str, owner_id: int, path: str, recipient_ids: list[int], workspace: Any
    ) -> dict[str, Any]:
        path = share_path(path)
        recipients = sorted(set(recipient_ids))
        if not recipients or len(recipients) > 100:
            raise OctopError(ErrorCode.SLASH_BAD_ARGS, "share requires 1 to 100 recipients")
        for uid in recipients:
            if isinstance(uid, bool) or uid <= 0 or self.services.user_repo.get(uid) is None:
                raise OctopError(ErrorCode.SLASH_BAD_ARGS, "unknown share recipient")
        data = await workspace.adownload_bytes(path)
        if data is None:
            raise OctopError(ErrorCode.NOT_FOUND, "share file not found")
        if len(data) > MAX_SHARE_BYTES:
            raise OctopError(ErrorCode.SLASH_BAD_ARGS, "share file exceeds 100 MiB")
        share_id = secrets.token_hex(24)
        meta = {
            "share_id": share_id,
            "agent_id": agent_id,
            "owner_id": owner_id,
            "recipient_ids": recipients,
            "filename": PurePosixPath(path).name,
            "path": path,
            "size": len(data),
            "revoked": False,
            "read_only": True,
            "snapshot": True,
        }

        def persist() -> None:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            with (self.root / share_id).open("xb") as target:
                target.write(data)
            self.settings.set(_PREFIX + share_id, json.dumps(meta))

        await asyncio.to_thread(persist)
        return meta

    def _metadata(self, share_id: str) -> dict[str, Any]:
        if len(share_id) != 48 or any(ch not in "0123456789abcdef" for ch in share_id):
            raise OctopError(ErrorCode.NOT_FOUND, "share not found")
        raw = self.settings.get(_PREFIX + share_id)
        if raw is None:
            raise OctopError(ErrorCode.NOT_FOUND, "share not found")
        return dict(json.loads(raw))

    def download(self, share_id: str, user_id: int) -> tuple[str, bytes]:
        meta = self.authorized_metadata(share_id, user_id)
        return str(meta["filename"]), (self.root / share_id).read_bytes()

    def authorized_metadata(self, share_id: str, user_id: int) -> dict[str, Any]:
        meta = self._metadata(share_id)
        if meta["revoked"] or user_id not in [meta["owner_id"], *meta["recipient_ids"]]:
            # Do not reveal whether another user's share exists.
            raise OctopError(ErrorCode.NOT_FOUND, "share not found")
        return meta

    def revoke(self, share_id: str, owner_id: int) -> None:
        meta = self._metadata(share_id)
        if meta["owner_id"] != owner_id:
            raise OctopError(ErrorCode.NOT_FOUND, "share not found")
        meta["revoked"] = True
        self.settings.set(_PREFIX + share_id, json.dumps(meta))
