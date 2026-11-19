"""Huihuo ERP organization directory synchronization and membership refresh."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from octop.config import YouliConfig
from octop.infra.auth.youli.client import YouliClient

logger = logging.getLogger(__name__)


class ErpOrgSyncService:
    """Sync department trees and employee memberships from Huihuo ERP."""

    def __init__(self, services: Any, cfg: YouliConfig) -> None:
        self._services = services
        self._cfg = cfg
        self._client = YouliClient(cfg)

    @property
    def enabled(self) -> bool:
        return self._client.enabled

    def sync_company(self, company_id: int) -> dict[str, Any]:
        directory = self._client.fetch_org_directory(company_id=int(company_id))
        org_repo = self._services.repos.erp_org_repo
        summary = org_repo.sync_company_directory(
            company_id=int(directory.company_id or company_id),
            departments=directory.departments,
            employees=directory.employees,
        )
        session_repo = getattr(self._services.repos, "youli_session_repo", None)
        if session_repo is not None:
            for uid in summary.get("revoked_user_ids") or []:
                session_repo.revoke_for_user(int(uid))
        return summary

    async def async_sync_company(self, company_id: int) -> dict[str, Any]:
        return await asyncio.to_thread(self.sync_company, company_id)

    def sync_all_known_companies(self) -> list[dict[str, Any]]:
        org_repo = self._services.repos.erp_org_repo
        results: list[dict[str, Any]] = []
        for company_id in org_repo.known_company_ids():
            results.append(self.sync_company(company_id))
        return results

    def refresh_user_membership(
        self, *, user_id: int, company_id: int, employee_id: int
    ) -> None:
        """Link pre-granted ERP identity and best-effort refresh department membership."""
        org_repo = getattr(self._services.repos, "erp_org_repo", None)
        if org_repo is None:
            return
        org_repo.link_user(company_id, employee_id, user_id)
        if not self._client.enabled:
            return
        try:
            # Fetch full departments if this company has not been synced yet; otherwise employee-only.
            has_depts = bool(org_repo.list_departments(company_id, include_inactive=True))
            if not has_depts:
                self.sync_company(company_id)
                return
            directory = self._client.fetch_org_directory(
                company_id=int(company_id),
                employee_id=int(employee_id),
                include_departments=False,
            )
            for emp in directory.employees:
                if int(emp.employee_id) == int(employee_id):
                    status = org_repo.sync_single_employee(
                        company_id,
                        emp,
                        user_id=user_id,
                    )
                    if status != 1:
                        session_repo = getattr(self._services.repos, "youli_session_repo", None)
                        if session_repo is not None:
                            session_repo.revoke_for_user(int(user_id))
                    break
        except Exception as exc:  # noqa: BLE001 - login must stay resilient when directory refresh fails
            logger.warning(
                "erp org membership refresh failed for user=%s company=%s employee=%s: %s",
                user_id,
                company_id,
                employee_id,
                exc,
            )

    async def async_refresh_user_membership(
        self, *, user_id: int, company_id: int, employee_id: int
    ) -> None:
        await asyncio.to_thread(
            self.refresh_user_membership,
            user_id=user_id,
            company_id=company_id,
            employee_id=employee_id,
        )
