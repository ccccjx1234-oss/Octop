"""PostgreSQL persistence for one user's isolated expert runtime."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Generator
from functools import lru_cache
from typing import Any, cast

import psycopg
from langgraph.checkpoint.base import CheckpointTuple
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from octop.infra.isolation.backend import UserWorkerBackend


def checkpoint_schema(runtime_id: str) -> str:
    if not re.fullmatch(r"u[0-9]+a[0-9a-f]{24}", runtime_id):
        raise ValueError("invalid isolated runtime id")
    return "cp_" + runtime_id


@lru_cache(maxsize=256)
def memory_backend(dsn: str, runtime_id: str) -> dict[str, str]:
    schema = checkpoint_schema(runtime_id)
    # Memory's business pool sets octop_memory itself. Its checkpoint pool
    # respects this search_path, keeping list/delete operations runtime-local.
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (schema,))
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
    options = conninfo_to_dict(dsn).get("options", "")
    scoped = make_conninfo(dsn, options=f"{options} -c search_path={schema},public".strip())
    return {"type": "postgres", "dsn": scoped}


class RuntimePostgresSaver(PostgresSaver):
    """Reuse LangGraph's format with bounded connections and async adapters."""

    def __init__(self, dsn: str):
        self._pool: ConnectionPool[psycopg.Connection[dict[str, Any]]] = ConnectionPool(
            dsn,
            min_size=0,
            max_size=2,
            timeout=10,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        super().__init__(self._pool)
        try:
            self.setup()
        except BaseException:
            self._pool.close()
            raise

    def close(self) -> None:
        self._pool.close()

    async def aget_tuple(self, config: Any) -> Any:
        return await asyncio.to_thread(self.get_tuple, config)

    async def aput(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self, config: Any, writes: Any, task_id: str, task_path: str = ""
    ) -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def alist(self, config: Any, **kwargs: Any) -> Any:
        iterator = cast(Generator[CheckpointTuple, None, None], self.list(config, **kwargs))
        sentinel = object()
        try:
            while True:
                item = await asyncio.to_thread(next, iterator, sentinel)
                if item is sentinel:
                    break
                yield item
        finally:
            await asyncio.to_thread(iterator.close)

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.delete_thread, thread_id)

    async def aget_delta_channel_history(self, **kwargs: Any) -> Any:
        return await asyncio.to_thread(self.get_delta_channel_history, **kwargs)


class PostgresWorkerBackend(UserWorkerBackend):
    """Close the explicit saver through the existing runtime lifecycle."""

    _checkpointer: RuntimePostgresSaver | None = None

    def checkpointer(self, dsn: str) -> RuntimePostgresSaver:
        with self._lock:
            if self._checkpointer is None:
                self._checkpointer = RuntimePostgresSaver(dsn)
            return self._checkpointer

    def close(self) -> None:
        super().close()
        with self._lock:
            if self._checkpointer is not None:
                self._checkpointer.close()
                self._checkpointer = None
