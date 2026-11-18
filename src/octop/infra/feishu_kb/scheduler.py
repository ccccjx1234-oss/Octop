"""Opt-in system scan for private Feishu sources."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from octop.infra.feishu_kb.service import FeishuKbService


def install_feishu_kb_job(cron: Any, *, server: Any) -> None:
    if os.environ.get("OCTOP_FEISHU_KB_SCHEDULER_ENABLED") != "1":
        return

    async def scan() -> None:
        if server.services is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, FeishuKbService(server.services).scan)

    cron.schedule_system_job("feishu-kb-scan", trigger="cron:*/15 * * * *", func=scan)
