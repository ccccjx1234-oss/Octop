"""Authenticated read-only shares; expert visibility does not grant file access."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field, StrictInt, StrictStr

from octop.api.common.content_disposition import content_disposition
from octop.api.common.workspace import require_agent_workspace
from octop.api.deps import current_user, get_server
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.isolation.context import enabled
from octop.infra.isolation.sharing import FileSharing

router = APIRouter()


class CreateFileShare(BaseModel):
    path: str = Field(..., min_length=1, description="Workspace-relative file path")
    recipient_ids: list[StrictInt] = Field(
        default_factory=list, max_length=100, description="Platform users allowed to download"
    )
    recipient_usernames: list[StrictStr] = Field(
        default_factory=list, max_length=100, description="Account names allowed to download"
    )


class FileShareResult(BaseModel):
    share_id: str
    agent_id: str
    filename: str
    size: int
    recipient_ids: list[int]
    read_only: bool
    snapshot: bool


def _service(server: Any) -> FileSharing:
    if not enabled():
        raise OctopError(ErrorCode.WORKSPACE_OP_UNSUPPORTED, "file shares require user isolation")
    return FileSharing(server.services)


@router.post(
    "/agents/{agent_id}/file-shares",
    response_model=FileShareResult,
    summary="Share a private file snapshot with named users",
)
async def create_share(
    agent_id: str,
    body: CreateFileShare,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    service = _service(server)
    workspace = await require_agent_workspace(agent_id, user=user, server=server)
    recipients = list(body.recipient_ids)
    for username in body.recipient_usernames:
        recipient = server.services.user_repo.get_by_username(username.strip())
        if recipient is None or recipient.disabled:
            raise OctopError(ErrorCode.SLASH_BAD_ARGS, "unknown share recipient")
        recipients.append(recipient.id)
    return await service.create(
        agent_id=agent_id,
        owner_id=user.id,
        path=body.path,
        recipient_ids=recipients,
        workspace=workspace,
    )


class ShareCapabilities(BaseModel):
    enabled: bool


@router.get(
    "/file-shares/capabilities",
    response_model=ShareCapabilities,
    summary="Check whether private file sharing is available",
)
async def capabilities(_: Any = Depends(current_user)) -> ShareCapabilities:
    return ShareCapabilities(enabled=enabled())


class SharedFileInfo(BaseModel):
    filename: str
    size: int
    can_revoke: bool
    read_only: bool = True
    snapshot: bool = True


@router.get(
    "/file-shares/{share_id}",
    response_model=SharedFileInfo,
    summary="Read metadata of an authorized file snapshot",
)
async def share_info(
    share_id: str,
    response: Response,
    user: Any = Depends(current_user),
    server: Any = Depends(get_server),
) -> SharedFileInfo:
    response.headers["Cache-Control"] = "private, no-store"
    meta = await asyncio.to_thread(_service(server).authorized_metadata, share_id, user.id)
    return SharedFileInfo(
        filename=meta["filename"], size=meta["size"], can_revoke=meta["owner_id"] == user.id
    )


@router.get(
    "/file-shares/{share_id}/download",
    summary="Download an authorized read-only snapshot",
    response_class=Response,
)
async def download_share(
    share_id: str, user: Any = Depends(current_user), server: Any = Depends(get_server)
) -> Response:
    filename, data = await asyncio.to_thread(_service(server).download, share_id, user.id)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": content_disposition(filename),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete(
    "/file-shares/{share_id}", status_code=204, summary="Revoke a file share as its owner"
)
async def revoke_share(
    share_id: str, user: Any = Depends(current_user), server: Any = Depends(get_server)
) -> Response:
    await asyncio.to_thread(_service(server).revoke, share_id, user.id)
    return Response(status_code=204)
