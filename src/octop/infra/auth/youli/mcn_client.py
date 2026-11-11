"""HTTP client for the MCN business APIs used by AIwork tools."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import httpx

from octop.config import YouliConfig

_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
_AUTH_CODES = frozenset({401, 403, 708})

logger = logging.getLogger(__name__)


class McnClientError(Exception):
    """The MCN API rejected the call or returned an unexpected body."""


class McnAuthExpired(McnClientError):
    """The delegated business token is no longer accepted."""


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

    @property
    def enabled(self) -> bool:
        return bool(self._base_url and self._token)

    @property
    def token_fingerprint(self) -> str:
        """Stable per-employee scope for in-process idempotency keys."""
        return hashlib.sha256(self._token.encode("utf-8")).hexdigest()

    def _request(
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
        try:
            with httpx.Client(timeout=_TIMEOUT) as client:
                response = client.request(
                    method, url, json=payload, params=params, headers=headers
                )
        except httpx.HTTPError as exc:
            raise McnClientError("MCN 服务不可达") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise McnClientError("MCN 返回了非 JSON 内容") from exc
        if not isinstance(body, dict):
            return body
        code = body.get("code")
        message = str(body.get("msg") or body.get("message") or "")
        if response.status_code >= 400 or (code is not None and int(code) != 200):
            if int(code or response.status_code) in _AUTH_CODES or response.status_code == 401:
                raise McnAuthExpired(message or "登录已失效，请重新进入")
            logger.warning(
                "MCN %s rejected: http=%s code=%s msg=%s",
                path,
                response.status_code,
                code,
                message,
            )
            raise McnClientError(message or "MCN 接口调用失败")
        if "data" in body:
            return body.get("data")
        return body

    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        return self._request("POST", path, payload=payload)

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)
