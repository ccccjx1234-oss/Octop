"""Youli (经贝 MCN) handoff requests, sessions and private-agent bindings."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

try:  # pragma: no cover - optional PostgreSQL driver
    from psycopg import errors as pg_errors
except ImportError:  # pragma: no cover
    pg_errors = None  # type: ignore[assignment]

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, now_ts


def _is_unique_violation(exc: BaseException) -> bool:
    if isinstance(exc, sqlite3.IntegrityError):
        return "unique" in str(exc).lower()
    return pg_errors is not None and isinstance(exc, pg_errors.UniqueViolation)


@dataclass(frozen=True)
class YouliLoginRequestRow:
    id: int
    request_id: str
    state_hash: str
    client_id: str
    audience: str
    callback_url: str
    verifier_hash: str
    verifier_blob: bytes | None
    browser_hash: str
    created_at: int
    expires_at: int
    consumed_at: int | None

    @classmethod
    def from_row(cls, row: DbRow) -> YouliLoginRequestRow:
        return cls(
            id=int(row["id"]),
            request_id=str(row["request_id"]),
            state_hash=str(row["state_hash"]),
            client_id=str(row["client_id"]),
            audience=str(row["audience"]),
            callback_url=str(row["callback_url"]),
            verifier_hash=str(row["verifier_hash"]),
            verifier_blob=(
                bytes(row["verifier_blob"]) if row["verifier_blob"] is not None else None
            ),
            browser_hash=str(row["browser_hash"]),
            created_at=int(row["created_at"]),
            expires_at=int(row["expires_at"]),
            consumed_at=(int(row["consumed_at"]) if row["consumed_at"] is not None else None),
        )


@dataclass(frozen=True)
class YouliSessionRow:
    id: int
    sid: str
    user_id: int
    account_user_id: int
    account_username: str
    company_id: int
    employee_id: int
    industry: int | None
    token_blob: bytes | None
    token_expires_at: int
    abs_expires_at: int
    status: str
    created_at: int
    revoked_at: int | None

    @property
    def active(self) -> bool:
        return self.status == "active"

    @classmethod
    def from_row(cls, row: DbRow) -> YouliSessionRow:
        blob = row["token_blob"]
        return cls(
            id=int(row["id"]),
            sid=str(row["sid"]),
            user_id=int(row["user_id"]),
            account_user_id=int(row["account_user_id"]),
            account_username=str(row["account_username"]),
            company_id=int(row["company_id"]),
            employee_id=int(row["employee_id"]),
            industry=(int(row["industry"]) if row["industry"] is not None else None),
            token_blob=bytes(blob) if blob is not None else None,
            token_expires_at=int(row["token_expires_at"]),
            abs_expires_at=int(row["abs_expires_at"]),
            status=str(row["status"] or "active"),
            created_at=int(row["created_at"]),
            revoked_at=(int(row["revoked_at"]) if row["revoked_at"] is not None else None),
        )


@dataclass(frozen=True)
class YouliAgentBindingRow:
    id: int
    company_id: int
    employee_id: int
    published_expert_id: int
    user_id: int
    agent_id: str
    youli_business: bool
    created_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> YouliAgentBindingRow:
        return cls(
            id=int(row["id"]),
            company_id=int(row["company_id"]),
            employee_id=int(row["employee_id"]),
            published_expert_id=int(row["published_expert_id"]),
            user_id=int(row["user_id"]),
            agent_id=str(row["agent_id"]),
            youli_business=bool(int(row["youli_business"] or 0)),
            created_at=int(row["created_at"]),
        )


@dataclass(frozen=True)
class YouliBusinessTemplateRow:
    id: int
    published_expert_id: int
    name: str
    created_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> YouliBusinessTemplateRow:
        return cls(
            id=int(row["id"]),
            published_expert_id=int(row["published_expert_id"]),
            name=str(row["name"] or ""),
            created_at=int(row["created_at"]),
        )


class YouliLoginRequestRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create(
        self,
        *,
        request_id: str,
        state_hash: str,
        client_id: str,
        audience: str,
        callback_url: str,
        verifier_hash: str,
        verifier_blob: bytes | None,
        browser_hash: str,
        expires_at: int,
    ) -> YouliLoginRequestRow:
        ts = now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO youli_login_request("
                "request_id, state_hash, client_id, audience, callback_url, "
                "verifier_hash, verifier_blob, browser_hash, created_at, expires_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    state_hash,
                    client_id,
                    audience,
                    callback_url,
                    verifier_hash,
                    verifier_blob,
                    browser_hash,
                    ts,
                    expires_at,
                ),
            )
        row = self.get(request_id)
        assert row is not None  # noqa: S101
        return row

    def get(self, request_id: str) -> YouliLoginRequestRow | None:
        with self._db.connect() as conn:
            row: Any = conn.execute(
                "SELECT * FROM youli_login_request WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return YouliLoginRequestRow.from_row(row) if row else None

    def consume(self, request_id: str) -> bool:
        """Atomically mark a pending request consumed; false when already used."""
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE youli_login_request SET consumed_at = ? "
                "WHERE request_id = ? AND consumed_at IS NULL AND expires_at > ?",
                (now_ts(), request_id, now_ts()),
            )
            return int(getattr(cur, "rowcount", 0) or 0) > 0

    def delete_expired(self, before: int | None = None) -> int:
        cutoff = now_ts() if before is None else int(before)
        with self._db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM youli_login_request WHERE expires_at < ?",
                (cutoff,),
            )
            return int(getattr(cur, "rowcount", 0) or 0)


class YouliSessionRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create(
        self,
        *,
        sid: str,
        user_id: int,
        account_user_id: int,
        account_username: str,
        company_id: int,
        employee_id: int,
        industry: int | None,
        token_blob: bytes | None,
        token_expires_at: int,
        abs_expires_at: int,
    ) -> YouliSessionRow:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO youli_session("
                "sid, user_id, account_user_id, account_username, company_id, employee_id, "
                "industry, token_blob, token_expires_at, abs_expires_at, "
                "status, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                (
                    sid,
                    user_id,
                    account_user_id,
                    account_username,
                    company_id,
                    employee_id,
                    industry,
                    token_blob,
                    token_expires_at,
                    abs_expires_at,
                    now_ts(),
                ),
            )
        row = self.get(sid)
        assert row is not None  # noqa: S101
        return row

    def get(self, sid: str) -> YouliSessionRow | None:
        with self._db.connect() as conn:
            row: Any = conn.execute(
                "SELECT * FROM youli_session WHERE sid = ?",
                (sid,),
            ).fetchone()
        return YouliSessionRow.from_row(row) if row else None

    def get_active(self, sid: str) -> YouliSessionRow | None:
        row = self.get(sid)
        if row is None or not row.active or row.abs_expires_at <= now_ts():
            return None
        return row

    def revoke(self, sid: str) -> bool:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE youli_session SET status = 'revoked', revoked_at = ?, token_blob = NULL "
                "WHERE sid = ? AND status = 'active'",
                (now_ts(), sid),
            )
            return int(getattr(cur, "rowcount", 0) or 0) > 0

    def revoke_for_user(self, user_id: int) -> int:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "UPDATE youli_session SET status = 'revoked', revoked_at = ?, token_blob = NULL "
                "WHERE user_id = ? AND status = 'active'",
                (now_ts(), user_id),
            )
            return int(getattr(cur, "rowcount", 0) or 0)

    def list_active_for_user(self, user_id: int) -> list[YouliSessionRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM youli_session WHERE user_id = ? AND status = 'active' "
                "AND abs_expires_at > ? ORDER BY created_at DESC",
                (user_id, now_ts()),
            ).fetchall()
        return map_rows(rows, YouliSessionRow)


class YouliAgentBindingRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def find(
        self, company_id: int, employee_id: int, published_expert_id: int
    ) -> YouliAgentBindingRow | None:
        with self._db.connect() as conn:
            row: Any = conn.execute(
                "SELECT * FROM youli_agent_binding "
                "WHERE company_id = ? AND employee_id = ? AND published_expert_id = ?",
                (company_id, employee_id, published_expert_id),
            ).fetchone()
        return YouliAgentBindingRow.from_row(row) if row else None

    def get_by_agent(self, agent_id: str) -> YouliAgentBindingRow | None:
        with self._db.connect() as conn:
            row: Any = conn.execute(
                "SELECT * FROM youli_agent_binding WHERE agent_id = ? ORDER BY id LIMIT 1",
                (agent_id,),
            ).fetchone()
        return YouliAgentBindingRow.from_row(row) if row else None

    def is_business_agent(self, agent_id: str) -> bool:
        row = self.get_by_agent(agent_id)
        return bool(row and row.youli_business)

    def create(
        self,
        *,
        company_id: int,
        employee_id: int,
        published_expert_id: int,
        user_id: int,
        agent_id: str,
    ) -> YouliAgentBindingRow:
        ts = now_ts()
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    "INSERT INTO youli_agent_binding("
                    "company_id, employee_id, published_expert_id, user_id, agent_id, "
                    "youli_business, created_at"
                    ") VALUES (?, ?, ?, ?, ?, 1, ?)",
                    (
                        company_id,
                        employee_id,
                        published_expert_id,
                        user_id,
                        agent_id,
                        ts,
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - unique race falls back to the row
            if not _is_unique_violation(exc):
                raise
        row = self.find(company_id, employee_id, published_expert_id)
        if row is None:
            raise RuntimeError("youli agent binding disappeared after insert")
        return row

    def delete_for_agent(self, agent_id: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "DELETE FROM youli_agent_binding WHERE agent_id = ?",
                (agent_id,),
            )


class YouliBusinessTemplateRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def add(self, published_expert_id: int, name: str = "") -> None:
        try:
            with self._db.transaction() as conn:
                conn.execute(
                    "INSERT INTO youli_business_template("
                    "published_expert_id, name, created_at) VALUES (?, ?, ?)",
                    (published_expert_id, name, now_ts()),
                )
        except Exception as exc:  # noqa: BLE001 - idempotent seed
            if not _is_unique_violation(exc):
                raise

    def is_business_template(self, published_expert_id: int) -> bool:
        with self._db.connect() as conn:
            row: Any = conn.execute(
                "SELECT 1 FROM youli_business_template WHERE published_expert_id = ?",
                (published_expert_id,),
            ).fetchone()
        return row is not None

    def list(self) -> list[YouliBusinessTemplateRow]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM youli_business_template ORDER BY id").fetchall()
        return map_rows(rows, YouliBusinessTemplateRow)
