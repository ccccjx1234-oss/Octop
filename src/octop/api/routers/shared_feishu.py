"""Feishu connections and explicitly approved knowledge-base directory imports."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from starlette.requests import Request
from pydantic import BaseModel, Field, model_validator

from octop.api.deps import get_server, require_permission
from octop.api.common.public_base import resolve_public_base
from octop.infra.feishu_kb.client import FeishuError
from octop.infra.feishu_kb.directories import DirectoryKind
from octop.infra.feishu_kb.shared_sources import (
    MAX_INTERVAL_MINUTES,
    MIN_INTERVAL_MINUTES,
    Scope,
    SharedFeishuService,
)
from octop.infra.users.identity import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/shared")
knowledge_user = require_permission("knowledge_bases")


def service(server: Any = Depends(get_server)) -> SharedFeishuService:
    return SharedFeishuService(server.services)


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except FeishuError as exc:
        status = 409 if exc.reason == "review_changed" else 400
        raise HTTPException(status_code=status, detail=exc.reason) from None
    except PermissionError:
        raise HTTPException(status_code=403, detail="knowledge_base_access_required") from None
    except LookupError:
        raise HTTPException(status_code=404, detail="knowledge_base_not_found") from None
    except (ValueError, RuntimeError):
        raise HTTPException(status_code=400, detail="knowledge_unavailable") from None


def _session(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="authentication_required")
    return authorization[7:]


class ConnectionRequest(BaseModel):
    provider_id: int
    label: str = Field(min_length=1, max_length=80)


class CallbackRequest(BaseModel):
    state: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=1, max_length=2048)


class DirectoryRequest(BaseModel):
    connection_id: str = Field(min_length=1, max_length=64)
    kind: DirectoryKind
    root: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=255)
    space: str | None = Field(default=None, max_length=200)
    include_children: bool = True

    def scope(self) -> Scope:
        return Scope(self.kind, self.root, self.title.strip(), self.space, self.include_children)


class ApproveRequest(DirectoryRequest):
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: Literal[True]
    schedule: Literal["manual", "daily", "interval"]
    interval_minutes: int | None = Field(
        default=None,
        ge=MIN_INTERVAL_MINUTES,
        le=MAX_INTERVAL_MINUTES,
        description="Sync interval in minutes when schedule is 'interval' (1-43200).",
    )
    auto_add: bool = False

    @model_validator(mode="after")
    def _validate_interval(self) -> ApproveRequest:
        if self.schedule == "interval" and self.interval_minutes is None:
            raise ValueError("interval_minutes is required when schedule is 'interval'")
        return self


class SourceUpdate(BaseModel):
    active: bool


@router.get("/connections", summary="List authorized Feishu accounts for the current user")
def connections(
    user: User = Depends(knowledge_user), svc: SharedFeishuService = Depends(service)
) -> dict[str, Any]:
    return {"connections": svc.connections(user.id), "providers": svc.providers_list()}


@router.post("/connections/authorize", summary="Authorize a Feishu account for directory imports")
def authorize(
    body: ConnectionRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    user: User = Depends(knowledge_user),
    svc: SharedFeishuService = Depends(service),
) -> dict[str, str]:
    with _errors():
        return {
            "url": svc.authorize(
                user.id,
                body.provider_id,
                _session(authorization),
                body.label,
                resolve_public_base(request),
            )
        }


@router.post("/connections/callback", summary="Finish authorization on the originating session")
def callback(
    body: CallbackRequest,
    authorization: str | None = Header(default=None),
    user: User = Depends(knowledge_user),
    svc: SharedFeishuService = Depends(service),
) -> dict[str, bool]:
    with _errors():
        svc.callback(user.id, _session(authorization), body.state, body.code)
    return {"ok": True}


@router.delete(
    "/connections/{connection_id}", summary="Disconnect an account and unpublish its imports"
)
def disconnect(
    connection_id: str,
    user: User = Depends(knowledge_user),
    svc: SharedFeishuService = Depends(service),
) -> dict[str, bool]:
    if not svc.repo.disconnect(user.id, connection_id, int(svc.clock())):
        raise HTTPException(status_code=404, detail="connection_unavailable")
    try:
        svc._cleanup()
    except Exception:
        logger.warning("Shared Feishu cleanup after disconnect will retry", exc_info=True)
    return {"ok": True}


@router.get(
    "/directories/{connection_id}", summary="Browse one page of Feishu spaces or child folders"
)
def browse(
    connection_id: str,
    kind: Literal["wiki_spaces", "wiki_nodes", "drive_folders"],
    parent: str | None = Query(default=None, max_length=200),
    space: str | None = Query(default=None, max_length=200),
    page: str | None = Query(default=None, max_length=2048),
    user: User = Depends(knowledge_user),
    svc: SharedFeishuService = Depends(service),
) -> dict[str, Any]:
    with _errors():
        return svc.browse(user.id, connection_id, kind, parent, space, page)


@router.get(
    "/bases/{kb_id}/sources", summary="List directory import jobs owned by this knowledge base"
)
def sources(
    kb_id: str, user: User = Depends(knowledge_user), svc: SharedFeishuService = Depends(service)
) -> list[dict[str, Any]]:
    with _errors():
        svc.knowledge.get_writable_base(kb_id, actor_user_id=user.id, is_admin=bool(user.is_admin))
        rows = svc.repo.sources(kb_id)
        if user.is_admin:
            return [dict(row) for row in rows]
        return [dict(row) for row in rows if row["owner_user_id"] == user.id]


@router.post(
    "/bases/{kb_id}/preview",
    summary="Review document names and knowledge-base visibility before import",
)
def preview(
    kb_id: str,
    body: DirectoryRequest,
    user: User = Depends(knowledge_user),
    svc: SharedFeishuService = Depends(service),
) -> dict[str, Any]:
    with _errors():
        return svc.preview(
            user.id,
            kb_id,
            body.connection_id,
            body.scope(),
            is_admin=bool(user.is_admin),
        )


@router.post(
    "/bases/{kb_id}/sources",
    status_code=202,
    summary="Queue an explicitly approved directory import",
)
def approve(
    kb_id: str,
    body: ApproveRequest,
    user: User = Depends(knowledge_user),
    svc: SharedFeishuService = Depends(service),
) -> dict[str, str]:
    with _errors():
        return {
            "id": svc.approve(
                user.id,
                kb_id,
                body.connection_id,
                body.scope(),
                body.fingerprint,
                schedule=body.schedule,
                auto_add=body.auto_add,
                interval_minutes=body.interval_minutes,
                is_admin=bool(user.is_admin),
            )
        }


@router.put("/bases/{kb_id}/sources/{source_id}", summary="Pause or queue a directory source")
def update_source(
    kb_id: str,
    source_id: str,
    body: SourceUpdate,
    user: User = Depends(knowledge_user),
    svc: SharedFeishuService = Depends(service),
) -> dict[str, bool]:
    with _errors():
        svc.knowledge.get_writable_base(kb_id, actor_user_id=user.id, is_admin=bool(user.is_admin))
        if not svc.repo.set_active(
            kb_id,
            source_id,
            user.id,
            body.active,
            int(svc.clock()),
            # Only an admin pausing skips connection ownership; resuming must belong to the OAuth owner.
            skip_owner_check=bool(user.is_admin) and not body.active,
        ):
            raise HTTPException(status_code=404, detail="source_unavailable")
    return {"ok": True}
