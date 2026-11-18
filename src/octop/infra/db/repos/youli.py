"""Youli (经贝 MCN) handoff requests, sessions and private-agent bindings."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

try:  # pragma: no cover - optional PostgreSQL driver
    from psycopg import errors as pg_errors
except ImportError:  # pragma: no cover
    pg_errors = None  # type: ignore[assignment]

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, insert_returning_id, map_rows, now_ts
from octop.infra.db.repos.users import UserRow


def lock_employee(conn: Any, dialect: str, company_id: int, employee_id: int) -> None:
    """Lock the same employee in every ownership transaction."""
    if dialect == "postgresql":
        digest = hashlib.sha256(f"youli:{company_id}:{employee_id}".encode()).digest()
        key = int.from_bytes(digest[:8], "big", signed=True)
        conn.execute("SELECT pg_advisory_xact_lock(?)", (key,))
    # SQLite transactions begin with BEGIN IMMEDIATE.


def lock_login_mapping(
    conn: Any, dialect: str, provider_id: int | None, subject: str | None, user_id: int | None
) -> None:
    if provider_id is None or subject is None:
        return
    provider = conn.execute("SELECT kind FROM sso_providers WHERE id = ?", (provider_id,)).fetchone()
    if provider is None or provider["kind"] != "youli":
        return
    parts = subject.split(":")
    if len(parts) != 3 or parts[0] != "youli" or not all(p.isdigit() for p in parts[1:]):
        raise ValueError("invalid employee identity")
    company_id, employee_id = int(parts[1]), int(parts[2])
    lock_employee(conn, dialect, company_id, employee_id)
    binding = conn.execute(
        "SELECT user_id, had_independent_identity FROM youli_identity_binding "
        "WHERE company_id = ? AND employee_id = ?", (company_id, employee_id),
    ).fetchone()
    if binding and (binding["had_independent_identity"] != 1 or binding["user_id"] != user_id):
        raise ValueError("employee identity reserved for delegation")


def lock_user_binding(conn: Any, dialect: str, user_id: int) -> Any:
    """Acquire employee before user; detect a binding changed during acquisition."""
    before = conn.execute(
        "SELECT company_id, employee_id FROM youli_identity_binding WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if before:
        lock_employee(conn, dialect, int(before["company_id"]), int(before["employee_id"]))
    if dialect == "postgresql":
        conn.execute("SELECT id FROM users WHERE id = ? FOR UPDATE", (user_id,)).fetchone()
    after = conn.execute(
        "SELECT * FROM youli_identity_binding WHERE user_id = ?", (user_id,)
    ).fetchone()
    old = (before["company_id"], before["employee_id"]) if before else None
    new = (after["company_id"], after["employee_id"]) if after else None
    if old != new:
        raise ValueError("binding changed; retry")
    return after


def revoke_binding(conn: Any, user_id: int, row: Any) -> None:
    conn.execute(
        "UPDATE youli_session SET status = 'revoked', revoked_at = ?, token_blob = NULL "
        "WHERE user_id = ? AND company_id = ? AND employee_id = ? AND status = 'active' "
        "AND (? != 1 OR sid = ?)",
        (now_ts(), user_id, row["company_id"], row["employee_id"],
         row["had_independent_identity"] or 0, row["sid"]),
    )
    # Unknown provenance remains quarantined; known bindings can be removed.
    unexpected_mapping = conn.execute(
        "SELECT 1 FROM user_sso_identities i JOIN sso_providers p ON p.id = i.provider_id "
        "WHERE p.kind = 'youli' AND i.subject = ? UNION ALL "
        "SELECT 1 FROM users u JOIN sso_providers p ON p.id = u.sso_provider_id "
        "WHERE p.kind = 'youli' AND u.sso_subject = ?",
        (f"youli:{row['company_id']}:{row['employee_id']}",) * 2,
    ).fetchone()
    if row["had_independent_identity"] == 1 or (
        row["had_independent_identity"] == 0 and not unexpected_mapping
    ):
        conn.execute("DELETE FROM youli_identity_binding WHERE user_id = ?", (user_id,))


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

    def login(self, *, provider_id: int, subject: str, role: str, role_name: str,
              permissions: list[str], **values: Any) -> tuple[UserRow, YouliSessionRow, list[str]]:
        """Resolve/create the ERP identity and replace its session under one employee lock."""
        with self._db.transaction() as conn:
            lock_employee(conn, self._db.dialect, values["company_id"], values["employee_id"])
            binding = conn.execute(
                "SELECT user_id, had_independent_identity FROM youli_identity_binding "
                "WHERE company_id = ? AND employee_id = ?",
                (values["company_id"], values["employee_id"]),
            ).fetchone()
            row = conn.execute(
                "SELECT u.* FROM users u JOIN user_sso_identities i ON i.user_id = u.id "
                "WHERE i.provider_id = ? AND i.subject = ?", (provider_id, subject),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM users WHERE sso_provider_id = ? AND sso_subject = ?",
                    (provider_id, subject),
                ).fetchone()
            if binding and (binding["had_independent_identity"] != 1 or row is None
                            or int(binding["user_id"]) != int(row["id"])):
                raise ValueError("employee identity reserved for delegation")
            if row is None:
                username = "sso_" + hashlib.sha256(subject.encode()).hexdigest()[:12]
                if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                    username += "_" + uuid.uuid4().hex[:12]
                user_id = insert_returning_id(
                    conn, "INSERT INTO users(username, password_hash, role, display_name, locale, "
                    "sso_provider_id, sso_subject, disabled, created_at, permissions, role_name) "
                    "VALUES (?, NULL, ?, ?, 'zh', ?, ?, 0, ?, ?, ?)",
                    (username, role, values["account_username"], provider_id, subject,
                     now_ts(), json.dumps(permissions), role_name),
                )
                conn.execute(
                    "INSERT INTO user_sso_identities(user_id, provider_id, subject, created_at) "
                    "VALUES (?, ?, ?, ?)", (user_id, provider_id, subject, now_ts()),
                )
                row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row["disabled"]:
                from octop.infra.errors import ErrorCode, OctopError
                raise OctopError(ErrorCode.USER_DISABLED, "user is disabled")
            user = UserRow.from_row(row)
            session, replaced = self._create_replacing(conn, user_id=user.id, **values)
            return user, session, replaced

    def create_replacing(self, **values: Any) -> tuple[YouliSessionRow, list[str]]:
        with self._db.transaction() as conn:
            lock_employee(conn, self._db.dialect, values["company_id"], values["employee_id"])
            binding = conn.execute(
                "SELECT user_id, had_independent_identity FROM youli_identity_binding "
                "WHERE company_id = ? AND employee_id = ?",
                (values["company_id"], values["employee_id"]),
            ).fetchone()
            if binding and (binding["had_independent_identity"] != 1
                            or int(binding["user_id"]) != values["user_id"]):
                raise ValueError("employee identity reserved for delegation")
            subject = f"youli:{values['company_id']}:{values['employee_id']}"
            identity = conn.execute(
                "SELECT 1 FROM users u JOIN sso_providers p ON p.kind = 'youli' "
                "WHERE u.id = ? AND ((u.sso_provider_id = p.id AND u.sso_subject = ?) "
                "OR EXISTS (SELECT 1 FROM user_sso_identities i WHERE i.user_id = u.id "
                "AND i.provider_id = p.id AND i.subject = ?))",
                (values["user_id"], subject, subject),
            ).fetchone()
            if not identity:
                raise ValueError("employee identity has no independent mapping")
            return self._create_replacing(conn, **values)

    def _create_replacing(self, conn: Any, **values: Any) -> tuple[YouliSessionRow, list[str]]:
        user_id, sid = values["user_id"], values["sid"]
        if self._db.dialect == "postgresql":
            conn.execute("SELECT id FROM users WHERE id = ? FOR UPDATE", (user_id,)).fetchone()
        rows = conn.execute(
            "SELECT sid FROM youli_session WHERE user_id = ? AND status = 'active'", (user_id,),
        ).fetchall()
        replaced = [str(row["sid"]) for row in rows]
        conn.execute(
            "UPDATE youli_session SET status = 'revoked', revoked_at = ?, token_blob = NULL "
            "WHERE user_id = ? AND status = 'active'", (now_ts(), user_id),
        )
        conn.execute(
            "INSERT INTO youli_session(sid, user_id, account_user_id, account_username, company_id, "
            "employee_id, industry, token_blob, token_expires_at, abs_expires_at, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
            tuple(values[k] for k in ("sid", "user_id", "account_user_id", "account_username",
                "company_id", "employee_id", "industry", "token_blob", "token_expires_at",
                "abs_expires_at")) + (now_ts(),),
        )
        row = conn.execute("SELECT * FROM youli_session WHERE sid = ?", (sid,)).fetchone()
        assert row is not None  # noqa: S101
        return YouliSessionRow.from_row(row), replaced

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
        if (
            row is None
            or not row.active
            or row.abs_expires_at <= now_ts()
            or row.token_expires_at <= now_ts()
        ):
            return None
        with self._db.connect() as conn:
            invalid = conn.execute(
                "SELECT 1 FROM youli_identity_binding b WHERE "
                "(b.user_id = ? AND b.company_id = ? AND b.employee_id = ? AND b.sid != ? "
                "AND (b.had_independent_identity IS NULL OR b.had_independent_identity = 0)) OR "
                "(b.sid = ? AND NOT EXISTS ("
                "SELECT 1 FROM user_sso_identities i JOIN sso_providers p ON p.id = i.provider_id "
                "WHERE i.user_id = b.user_id AND p.kind = 'feishu' "
                "AND i.subject = b.feishu_subject))",
                (row.user_id, row.company_id, row.employee_id, sid, sid),
            ).fetchone()
        return None if invalid else row

    def is_bound_sid(self, sid: str) -> bool:
        with self._db.connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM youli_identity_binding WHERE sid = ?", (sid,)
                ).fetchone()
                is not None
            )

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

    def revoke_other_for_user(self, user_id: int, *, keep_sid: str) -> list[str]:
        """Revoke any other active Youli sessions for *user_id* and return their sids."""
        with self._db.transaction() as conn:
            rows = conn.execute(
                "SELECT sid FROM youli_session WHERE user_id = ? AND status = 'active' AND sid != ?",
                (user_id, keep_sid),
            ).fetchall()
            sids = [str(row["sid"] if isinstance(row, dict) else row[0]) for row in rows]
            if not sids:
                return []
            conn.execute(
                "UPDATE youli_session SET status = 'revoked', revoked_at = ?, token_blob = NULL "
                "WHERE user_id = ? AND status = 'active' AND sid != ?",
                (now_ts(), user_id, keep_sid),
            )
            return sids

    def list_active_for_user(self, user_id: int) -> list[YouliSessionRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM youli_session WHERE user_id = ? AND status = 'active' "
                "AND abs_expires_at > ? AND token_expires_at > ? ORDER BY created_at DESC",
                (user_id, now_ts(), now_ts()),
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


class YouliIdentityBindingRepo:
    """Atomic ownership, identity mapping, and delegated session changes."""

    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def get(self, user_id: int) -> Any | None:
        with self._db.connect() as conn:
            return conn.execute(
                "SELECT * FROM youli_identity_binding WHERE user_id = ?", (user_id,)
            ).fetchone()

    def is_reserved(self, company_id: int, employee_id: int) -> bool:
        with self._db.connect() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM youli_identity_binding WHERE company_id = ? AND employee_id = ?",
                    (company_id, employee_id),
                ).fetchone()
                is not None
            )

    def begin_attempt(self, user_id: int) -> int:
        with self._db.transaction() as conn:
            conn.execute(
                "INSERT INTO youli_binding_generation(user_id, generation) VALUES (?, 0) "
                "ON CONFLICT(user_id) DO NOTHING",
                (user_id,),
            )
            row = conn.execute(
                "SELECT generation FROM youli_binding_generation WHERE user_id = ?", (user_id,)
            ).fetchone()
            return int(row["generation"])

    def bind(self, *, user_id: int, provider_id: int, subject: str, **values: Any) -> None:
        company_id, employee_id = values["company_id"], values["employee_id"]
        try:
            with self._db.transaction() as conn:
                lock_employee(conn, self._db.dialect, company_id, employee_id)
                if self._db.dialect == "postgresql":
                    conn.execute(
                        "SELECT id FROM users WHERE id = ? FOR UPDATE", (user_id,)
                    ).fetchone()
                from octop.infra.db.repos.feishu_erp_reauth import FeishuErpReauthRepo

                claim = values["feishu_claim"]
                reauth = FeishuErpReauthRepo(self._db)
                reauth._lock(conn, user_id, int(claim["provider_id"]))
                if (
                    int(claim["user_id"]) != user_id
                    or values["feishu_subject"] != claim["subject"]
                    or not reauth._valid(conn, claim)
                ):
                    raise ValueError("binding attempt superseded")
                claimed = conn.execute(
                    "UPDATE youli_binding_generation SET generation = generation + 1 "
                    "WHERE user_id = ? AND generation = ?",
                    (user_id, claim["generation"]),
                )
                if claimed.rowcount != 1:
                    raise ValueError("binding attempt superseded")
                current = conn.execute(
                    "SELECT * FROM youli_identity_binding WHERE user_id = ?", (user_id,)
                ).fetchone()
                if current and (
                    (current["company_id"], current["employee_id"]) != (company_id, employee_id)
                    or current["feishu_subject"] != values["feishu_subject"]
                ):
                    raise ValueError("different identity already linked")
                identity = conn.execute(
                    "SELECT user_id, subject FROM user_sso_identities WHERE provider_id = ? "
                    "AND (subject = ? OR user_id = ?)",
                    (provider_id, subject, user_id),
                ).fetchall()
                if any(
                    int(row["user_id"]) != user_id or row["subject"] != subject for row in identity
                ):
                    raise ValueError("business identity already linked")
                legacy = conn.execute(
                    "SELECT id, sso_subject FROM users WHERE sso_provider_id = ? "
                    "AND (sso_subject = ? OR id = ?)",
                    (provider_id, subject, user_id),
                ).fetchall()
                if any(
                    int(row["id"]) != user_id or row["sso_subject"] != subject for row in legacy
                ):
                    raise ValueError("business identity already linked")
                owner = conn.execute(
                    "SELECT user_id FROM youli_identity_binding "
                    "WHERE company_id = ? AND employee_id = ? AND user_id != ?",
                    (company_id, employee_id, user_id),
                ).fetchone()
                if owner:
                    raise ValueError("business identity already linked")
                if current:
                    conn.execute(
                        "UPDATE youli_session SET status = 'revoked', revoked_at = ?, token_blob = NULL "
                        "WHERE sid = ? AND user_id = ?",
                        (now_ts(), current["sid"], user_id),
                    )
                conn.execute(
                    "INSERT INTO youli_session(sid, user_id, account_user_id, account_username, "
                    "company_id, employee_id, industry, token_blob, token_expires_at, abs_expires_at, "
                    "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                    (
                        values["sid"],
                        user_id,
                        values["account_user_id"],
                        values["account_username"],
                        company_id,
                        employee_id,
                        values["industry"],
                        values["token_blob"],
                        values["token_expires_at"],
                        values["abs_expires_at"],
                        now_ts(),
                    ),
                )
                conn.execute(
                    "INSERT INTO youli_identity_binding(user_id, company_id, employee_id, feishu_subject, sid, "
                    "had_independent_identity) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET sid = excluded.sid",
                    (user_id, company_id, employee_id, values["feishu_subject"], values["sid"],
                     int(bool(identity or legacy))),
                )
        except Exception as exc:
            if _is_unique_violation(exc):
                raise ValueError("business identity already linked") from exc
            raise

    def unlink(self, user_id: int, provider_id: int) -> bool:
        with self._db.transaction() as conn:
            row = lock_user_binding(conn, self._db.dialect, user_id)
            conn.execute(
                "INSERT INTO youli_binding_generation(user_id, generation) VALUES (?, 1) "
                "ON CONFLICT(user_id) DO UPDATE SET generation = youli_binding_generation.generation + 1",
                (user_id,),
            )
            if row is None:
                return False
            revoke_binding(conn, user_id, row)
            return True
