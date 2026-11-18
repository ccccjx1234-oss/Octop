"""Helpers shared across repo implementations."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, TypeVar

R_co = TypeVar("R_co", covariant=True)

UNSET = object()

# Row mapping returned by the DB driver (sqlite3.Row today; asyncpg.Record later).
DbRow = Mapping[str, Any]


class _FromRow(Protocol[R_co]):
    @classmethod
    def from_row(cls, row: DbRow) -> R_co: ...


def now_ts() -> int:
    return int(time.time())


def bool_int(value: bool) -> int:
    return 1 if value else 0


def map_rows[R_co](rows: Sequence[DbRow], cls: type[_FromRow[R_co]]) -> list[R_co]:
    return [cls.from_row(r) for r in rows]


def partial_updates(pairs: list[tuple[str, object | None]]) -> tuple[list[str], list[object]]:
    """Build SET clauses for optional fields (None = skip)."""
    clauses: list[str] = []
    params: list[object] = []
    for col, val in pairs:
        if val is None:
            continue
        clauses.append(f"{col} = ?")
        params.append(val)
    return clauses, params


def optional_updates(
    pairs: list[tuple[str, object]],
    *,
    sentinel: object = UNSET,
) -> tuple[list[str], list[object]]:
    """Build SET clauses; sentinel means the field was omitted from the patch."""
    clauses: list[str] = []
    params: list[object] = []
    for col, val in pairs:
        if val is sentinel:
            continue
        clauses.append(f"{col} = ?")
        params.append(val)
    return clauses, params


def sql_in_placeholders(count: int) -> str:
    """``?, ?, ?`` for IN (...) clauses; swap ``?`` → ``%s`` when adding PostgreSQL."""
    return ", ".join("?" * count)


def sql_unix_day_bucket(column: str, *, dialect: str = "sqlite") -> str:
    """Expression that buckets a unix-epoch integer column into YYYY-MM-DD."""
    if dialect == "postgresql":
        return f"to_char(to_timestamp({column}), 'YYYY-MM-DD')"
    return f"date({column}, 'unixepoch')"


def insert_returning_id(conn: Any, sql: str, params: Sequence[object]) -> int:
    """Run INSERT … RETURNING id (SQLite 3.35+ and PostgreSQL)."""
    row = conn.execute(f"{sql} RETURNING id", params).fetchone()
    if row is None:
        raise RuntimeError("INSERT RETURNING id returned no row")
    if isinstance(row, Mapping):
        return int(row["id"])
    return int(row[0])


def lock_thread(conn: Any, dialect: str, thread_id: str) -> Any:
    """Serialize recipient changes with private-thread protection."""
    suffix = " FOR UPDATE" if dialect == "postgresql" else ""
    return conn.execute(
        "SELECT user_id, agent_id, private_knowledge_protected FROM threads WHERE thread_id = ?"
        + suffix,
        (thread_id,),
    ).fetchone()


class PrivateThreadBindingError(ValueError):
    """A non-dashboard recipient cannot bind a private thread."""


class PrivateThreadForkError(ValueError):
    """A protected source cannot be forked."""
