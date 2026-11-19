"""Owner-only Feishu document staging API."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field

from octop.api.deps import current_user, get_server
from octop.api.routers.shared_feishu import router as shared_router
from octop.infra.feishu_kb.client import FeishuError
from octop.infra.feishu_kb.service import FeishuKbService
from octop.infra.users.identity import User

router = APIRouter()
router.include_router(shared_router)


class AuthorizeRequest(BaseModel):
    provider_id: int


class CallbackRequest(BaseModel):
    state: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=1, max_length=2048)


class SourceRequest(BaseModel):
    provider_id: int
    document_id: str = Field(min_length=1, max_length=200)


class SourceConfig(BaseModel):
    enabled: bool
    interval_seconds: int = Field(ge=900, le=2592000)
    retrieval_enabled: bool | None = None


def _session(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="authentication required")
    return authorization[7:]


def _call(func: Any, *args: Any) -> Any:
    try:
        return func(*args)
    except FeishuError as exc:
        raise HTTPException(status_code=400, detail=exc.reason) from None


@router.get("/status", summary="List linked Feishu identities and private staging status")
def status(user: User = Depends(current_user), server: Any = Depends(get_server)) -> dict[str, Any]:
    return {
        "identities": FeishuKbService(server.services).status(user.id),
        "scheduler_configured": os.environ.get("OCTOP_FEISHU_KB_SCHEDULER_ENABLED") == "1",
    }


@router.post("/authorize", summary="Begin owner-bound Feishu document authorization")
def authorize(
    body: AuthorizeRequest,
    authorization: str | None = Header(None),
    user: User = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, str]:
    return {
        "url": _call(
            FeishuKbService(server.services).authorize,
            user.id,
            body.provider_id,
            _session(authorization),
        )
    }


@router.post("/callback", summary="Complete Feishu document authorization")
def callback(
    body: CallbackRequest,
    authorization: str | None = Header(None),
    user: User = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, bool]:
    _call(
        FeishuKbService(server.services).callback,
        user.id,
        _session(authorization),
        body.state,
        body.code,
    )
    return {"ok": True}


@router.post("/sources", summary="Select a Feishu document (initially disabled)")
def create(
    body: SourceRequest, user: User = Depends(current_user), server: Any = Depends(get_server)
) -> dict[str, str]:
    return {
        "source_id": _call(
            FeishuKbService(server.services).create, user.id, body.provider_id, body.document_id
        )
    }


@router.get("/sources", summary="List private Feishu sources and status")
def sources(
    user: User = Depends(current_user), server: Any = Depends(get_server)
) -> list[dict[str, Any]]:
    safe_statuses = {
        "pending",
        "updated",
        "unchanged",
        "forbidden",
        "missing",
        "unavailable",
        "upstream_error",
        "invalid_response",
        "invalid_identity",
        "identity_unavailable",
        "grant_unavailable",
        "provider_unavailable",
        "token_unavailable",
        "invalid_token_response",
        "document_too_large",
        "invalid_document_id",
    }
    result = []
    for row in FeishuKbService(server.services).repo.sources(user.id):
        item = dict(row)
        item["has_staged_content"] = bool(item.pop("digest", None))
        if item["status"] not in safe_statuses:
            item["status"] = "error"
        result.append(item)
    return result


@router.put("/sources/{source_id}", summary="Configure a private Feishu source")
def configure(
    source_id: str,
    body: SourceConfig,
    user: User = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, bool]:
    _call(
        FeishuKbService(server.services).configure,
        user.id,
        source_id,
        body.enabled,
        body.interval_seconds,
        body.retrieval_enabled,
    )
    return {"ok": True}


@router.post("/sources/{source_id}/sync", summary="Synchronize one Feishu document")
def sync(
    source_id: str, user: User = Depends(current_user), server: Any = Depends(get_server)
) -> dict[str, str]:
    return {"status": _call(FeishuKbService(server.services).sync, user.id, source_id)}


@router.get(
    "/sources/{source_id}/content",
    summary="Read a staged document after a live Feishu permission check",
)
def content(
    source_id: str,
    response: Response,
    user: User = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    return {"content": _call(FeishuKbService(server.services).read, user.id, source_id)}


@router.delete("/sources/{source_id}", summary="Delete a private Feishu source")
def delete(
    source_id: str, user: User = Depends(current_user), server: Any = Depends(get_server)
) -> dict[str, bool]:
    deleted = FeishuKbService(server.services).repo.delete_source(source_id, user.id)
    from octop.infra.feishu_kb.private_index import cleanup

    cleanup(server.services)
    return {"deleted": deleted}


@router.delete(
    "/grants/{provider_id}", summary="Revoke a Feishu document grant and delete its sources"
)
def revoke(
    provider_id: int, user: User = Depends(current_user), server: Any = Depends(get_server)
) -> dict[str, bool]:
    FeishuKbService(server.services).repo.revoke(user.id, provider_id)
    from octop.infra.feishu_kb.private_index import cleanup

    cleanup(server.services)
    return {"ok": True}
