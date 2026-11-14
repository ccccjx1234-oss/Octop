"""HTTP client for the MCN business APIs used by AIwork tools."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import httpx

from octop.config import YouliConfig
from octop.infra.auth.youli.tls import build_verify

_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
_AUTH_CODES = frozenset({401, 403, 708})

logger = logging.getLogger(__name__)


class McnClientError(Exception):
    """The MCN API rejected the call or returned an unexpected body."""


class McnAuthExpired(McnClientError):
    """The delegated business token is no longer accepted."""


class McnPermissionError(McnClientError):
    """The authenticated user lacks role or data permission for this endpoint."""


def _parse_int_code(code: Any) -> int | None:
    if isinstance(code, bool) or code is None:
        return None
    if isinstance(code, int):
        return code
    try:
        return int(str(code).strip())
    except (TypeError, ValueError):
        return None


class McnClient:
    def __init__(
        self,
        cfg: YouliConfig | None,
        token: str,
        *,
        base_url: str = "",
    ) -> None:
        self._base_url = (base_url or (cfg.mcn_base_url if cfg is not None else "")).strip()
        self._token = token
        self._tls = build_verify(cfg.ca_file if cfg is not None else "")

    @property
    def enabled(self) -> bool:
        return bool(self._base_url and self._token)

    @property
    def token_fingerprint(self) -> str:
        """Stable per-employee scope for in-process idempotency keys."""
        return hashlib.sha256(self._token.encode("utf-8")).hexdigest()

    def _request_envelope(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if not self.enabled:
            raise McnClientError("MCN 业务接口未配置")
        url = f"{self._base_url.rstrip('/')}{path}"
        headers = {"Auth-Token": self._token}
        from octop.infra.auth.youli.http_pool import perform_request

        try:
            response = perform_request(
                httpx.Client,
                method,
                url,
                timeout=_TIMEOUT,
                verify=self._tls,
                json=payload,
                params=params,
                headers=headers,
                target_label=f"mcn:{path}",
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "mcn transport error method=%s path=%s token_fp=%s exc_type=%s detail=%s",
                method,
                path,
                self.token_fingerprint,
                type(exc).__name__,
                exc,
            )
            raise McnClientError(f"MCN 服务不可达 ({type(exc).__name__})") from exc
        try:
            body = response.json()
        except ValueError as exc:
            logger.warning(
                "mcn non-json response method=%s path=%s http_status=%s token_fp=%s",
                method,
                path,
                response.status_code,
                self.token_fingerprint,
            )
            raise McnClientError("MCN 返回了非 JSON 内容") from exc
        if not isinstance(body, dict):
            if response.status_code == 401:
                raise McnAuthExpired("登录已失效，请重新进入")
            if response.status_code == 403:
                raise McnPermissionError("当前账号暂无该接口查看权限")
            if response.status_code >= 400:
                raise McnClientError(f"MCN 接口调用失败 (HTTP {response.status_code})")
            return body
        code = body.get("code")
        int_code = _parse_int_code(code)
        message = str(body.get("msg") or body.get("message") or "")
        is_non_ok_code = (
            (int_code is not None and int_code != 200)
            or (code is not None and int_code is None and str(code).strip().upper() not in {"200", "OK", "SUCCESS"})
        )
        if response.status_code >= 400 or is_non_ok_code:
            logger.warning(
                "mcn business/http error method=%s path=%s http_status=%s biz_code=%s token_fp=%s msg=%s",
                method,
                path,
                response.status_code,
                code,
                self.token_fingerprint,
                message,
            )
            if response.status_code == 401 or (int_code is not None and int_code in _AUTH_CODES):
                raise McnAuthExpired(message or "登录已失效，请重新进入")
            if response.status_code == 403:
                if "登录" in message or "token" in message.lower():
                    raise McnAuthExpired(message or "登录已失效，请重新进入")
                raise McnPermissionError(message or "当前账号暂无该接口查看权限")
            logger.warning(
                "MCN %s rejected: http=%s code=%s msg=%s",
                path,
                response.status_code,
                code,
                message,
            )
            raise McnClientError(message or "MCN 接口调用失败")
        return body

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        body = self._request_envelope(method, path, payload=payload, params=params)
        if isinstance(body, dict) and "data" in body:
            return body.get("data")
        return body

    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        return self._request("POST", path, payload=payload)

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def post_with_meta(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        """POST keeping outer pagination metadata (e.g. top-level ``page.totalCount``)."""
        if "post" in self.__dict__ or type(self).post is not _ORIG_MCN_POST:
            res = self.post(path, payload)
            return res if isinstance(res, dict) and ("data" in res or "page" in res) else {"data": res}
        return self._request_envelope("POST", path, payload=payload)

    def get_with_meta(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET keeping outer pagination metadata when available."""
        if "get" in self.__dict__ or type(self).get is not _ORIG_MCN_GET:
            res = self.get(path, params)
            return res if isinstance(res, dict) and ("data" in res or "page" in res) else {"data": res}
        return self._request_envelope("GET", path, params=params)


_ORIG_MCN_POST = McnClient.post
_ORIG_MCN_GET = McnClient.get
