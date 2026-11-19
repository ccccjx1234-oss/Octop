"""Youli (经贝 MCN) handoff login endpoints."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, Field

from octop.api.deps import current_user, get_server
from octop.infra.auth.youli.reauth import FeishuErpReauth, denied
from octop.infra.auth.youli.service import YouliAuthService
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.users.identity import User

logger = logging.getLogger(__name__)

router = APIRouter()


class YouliExchangeBody(BaseModel):
    code: str = Field(max_length=512)
    state: str = Field(max_length=512)
    request_id: str = Field(max_length=64)


class YouliStaffLoginBody(BaseModel):
    mobile: str = Field(min_length=5, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class YouliBindingBody(YouliStaffLoginBody):
    proof: str = Field(min_length=1, max_length=512)


class ReauthStartBody(BaseModel):
    provider_id: int


class ReauthCallbackBody(BaseModel):
    state: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=1, max_length=2048)


def _bearer(authorization: str | None) -> str:
    if (
        not authorization
        or not authorization.lower().startswith("bearer ")
        or not authorization[7:]
    ):
        raise denied()
    return authorization[7:]


def _reauth(server: Any) -> FeishuErpReauth:
    return FeishuErpReauth(server.services, server.sso_service)


@router.post("/binding/reauth/start", summary="Start Feishu OAuth verification for ERP binding")
def reauth_start(
    body: ReauthStartBody,
    response: Response,
    authorization: str | None = Header(None),
    user: User = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    return {
        "authorization_url": _reauth(server).start(
            user.id, _bearer(authorization), body.provider_id
        )
    }


@router.post(
    "/binding/reauth/callback", summary="Complete Feishu OAuth verification for ERP binding"
)
def reauth_callback(
    body: ReauthCallbackBody,
    response: Response,
    authorization: str | None = Header(None),
    user: User = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    return {
        "proof": _reauth(server).callback(user.id, _bearer(authorization), body.state, body.code)
    }


class YouliBindingStatus(BaseModel):
    bound: bool
    delegation_active: bool = Field(
        description="An unexpired local delegation exists; MDM is checked on use."
    )
    company_id: int | None = None
    employee_id: int | None = None


def _service(server: Any) -> YouliAuthService:
    app_runtime = getattr(server, "app_runtime", None)
    gateway = getattr(app_runtime, "gateway", None) if app_runtime is not None else None
    return YouliAuthService(
        services=server.services,
        users=server.user_manager,
        cfg=server.services.config.youli,
        ws_hub=getattr(gateway, "ws_hub", None),
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
    from octop.api.routers.auth import me_payload

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
    from octop.api.routers.auth import me_payload

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


@router.get(
    "/binding",
    summary="Get your ERP binding status",
    response_model=YouliBindingStatus,
    response_model_exclude_none=True,
)
async def binding_status(
    response: Response, user: User = Depends(current_user), server: Any = Depends(get_server)
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return _service(server).binding_status(user.id)


async def _bind_verified(
    body: YouliBindingBody,
    response: Response,
    authorization: str | None,
    user: User,
    server: Any,
) -> dict[str, Any]:
    """Bind a verified Feishu session to an ERP delegation."""
    response.headers["Cache-Control"] = "no-store"
    claim = _reauth(server).claim(user.id, _bearer(authorization), body.proof)
    service = _service(server)
    await service.bind_employee(
        user_id=user.id,
        mobile=body.mobile.strip(),
        password=body.password,
        feishu_claim=claim,
    )
    return service.binding_status(user.id)


@router.post(
    "/binding",
    summary="Bind your ERP account after Feishu verification",
    response_model=YouliBindingStatus,
    response_model_exclude_none=True,
)
async def bind_employee(
    body: YouliBindingBody,
    response: Response,
    authorization: str | None = Header(None),
    user: User = Depends(current_user),
    server: Any = Depends(get_server),
) -> dict[str, Any]:
    return await _bind_verified(body, response, authorization, user, server)


@router.delete(
    "/binding",
    summary="Unlink your ERP account and revoke its delegation",
    response_model=YouliBindingStatus,
    response_model_exclude_none=True,
)
async def unlink_employee(
    response: Response, user: User = Depends(current_user), server: Any = Depends(get_server)
) -> dict[str, bool]:
    await _service(server).unlink_employee(user.id)
    response.headers["Cache-Control"] = "no-store"
    return {"bound": False, "delegation_active": False}
