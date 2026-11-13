"""Youli (经贝 MCN) handoff login endpoints."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from octop.api.deps import get_server
from octop.api.routers.auth import me_payload
from octop.infra.auth.youli.service import YouliAuthService
from octop.infra.errors import ErrorCode, OctopError

logger = logging.getLogger(__name__)

router = APIRouter()


class YouliExchangeBody(BaseModel):
    code: str = Field(max_length=512)
    state: str = Field(max_length=512)
    request_id: str = Field(max_length=64)


class YouliStaffLoginBody(BaseModel):
    mobile: str = Field(min_length=5, max_length=32)
    password: str = Field(min_length=1, max_length=128)


def _service(server: Any) -> YouliAuthService:
    return YouliAuthService(
        services=server.services,
        users=server.user_manager,
        cfg=server.services.config.youli,
    )


@router.get("/status", summary="Youli handoff availability")
async def status(server: Any = Depends(get_server)) -> dict[str, Any]:
    cfg = server.services.config.youli
    return {
        "enabled": bool(cfg.enabled and cfg.base_url and cfg.callback_url),
        "audience": cfg.audience,
    }


@router.post("/start", summary="Start Youli handoff login")
async def start(
    request: Request,
    response: Response,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    service = _service(server)
    result = await service.start(public_base=str(request.base_url).rstrip("/"))
    response.set_cookie(
        key=result.cookie_name,
        value=result.browser_token,
        max_age=result.max_age,
        httponly=True,
        secure=bool(server.services.config.youli.cookie_secure),
        samesite="lax",
        path=result.cookie_path,
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "request_id": result.request_id,
        "state": result.state,
        "entry_url": result.entry_url,
        "callback_url": result.callback_url,
        "expires_in": result.max_age,
    }


@router.post("/staff-login", summary="Sign in an MCN employee from the HuiHuo page")
async def staff_login(
    body: YouliStaffLoginBody,
    request: Request,
    response: Response,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    """Validate the employee password upstream, then mint an Octop session."""
    service = _service(server)
    result = await service.staff_login(
        mobile=body.mobile.strip(),
        password=body.password,
        public_base=str(request.base_url).rstrip("/"),
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "access_token": result.access_token,
        "token_type": "Bearer",
        "expires_in": result.expires_in,
        "user": me_payload(result.user, server),
    }


@router.post("/exchange", summary="Exchange Youli handoff code")
async def exchange(
    body: YouliExchangeBody,
    request: Request,
    response: Response,
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    service = _service(server)
    cookie = request.cookies.get(f"youli_sso_{body.request_id}")
    if not cookie:
        raise OctopError(ErrorCode.AUTH_FAILED, "login request is not bound to this browser")
    result = await service.exchange(
        code=body.code,
        state=body.state,
        request_id=body.request_id,
        browser_token=cookie,
    )
    response.delete_cookie(
        key=f"youli_sso_{body.request_id}",
        path=service._cookie_path(),  # noqa: SLF001 - same module owns the path
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "access_token": result.access_token,
        "token_type": "Bearer",
        "expires_in": result.expires_in,
        "user": me_payload(result.user, server),
    }
