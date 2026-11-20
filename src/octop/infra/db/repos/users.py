"""User table access."""

from __future__ import annotations

import builtins
import json
from dataclasses import dataclass, field

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, bool_int, insert_returning_id, map_rows, now_ts


def _parse_permissions(raw: object) -> builtins.list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "[]")
        except (ValueError, TypeError):
            return []
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
        return []
    return []


@dataclass(frozen=True)
class UserSsoIdentityRow:
    user_id: int
    provider_id: int
    subject: str
    kind: str
    created_at: int

    @classmethod
    def from_row(cls, r: DbRow) -> UserSsoIdentityRow:
        return cls(
            user_id=int(r["user_id"]),
            provider_id=int(r["provider_id"]),
            subject=str(r["subject"]),
            kind=str(r["kind"]),
            created_at=int(r["created_at"]),
        )


@dataclass(frozen=True)
class UserRow:
    id: int
    username: str
    password_hash: str | None
    role: str
    display_name: str | None
    disabled: int
    created_at: int
    locale: str
    email: str | None
    sso_provider_id: int | None
    sso_subject: str | None
    preferences_json: str = "{}"
    login_failed_count: int = 0
    login_locked_until: int = 0
    mfa_version: int = 0
    mfa_failed_count: int = 0
    mfa_locked_until: int = 0
    password_version: int = 0
    permissions: builtins.list[str] = field(default_factory=list)
    role_name: str | None = None
    avatar_icon: str | None = None
    last_login_at: int | None = None
    mobile: str | None = None

    @classmethod
    def from_row(cls, r: DbRow) -> UserRow:
        keys = set(r.keys())
        raw_name = r["role_name"] if "role_name" in keys else None
        role_name = str(raw_name).strip() if isinstance(raw_name, str) else ""
        raw_icon = r["avatar_icon"] if "avatar_icon" in keys else None
        avatar_icon = str(raw_icon).strip() if isinstance(raw_icon, str) else ""
        return cls(
            id=r["id"],
            username=r["username"],
            password_hash=r["password_hash"],
            role=r["role"],
            display_name=r["display_name"],
            disabled=r["disabled"],
            created_at=r["created_at"],
            locale=r["locale"],
            preferences_json=str(r["preferences_json"] or "{}"),
            login_failed_count=int(r["login_failed_count"] or 0),
            login_locked_until=int(r["login_locked_until"] or 0),
            mfa_version=int(r["mfa_version"] or 0) if "mfa_version" in keys else 0,
            mfa_failed_count=int(r["mfa_failed_count"] or 0) if "mfa_failed_count" in keys else 0,
            mfa_locked_until=int(r["mfa_locked_until"] or 0) if "mfa_locked_until" in keys else 0,
            password_version=int(r["password_version"] or 0) if "password_version" in keys else 0,
            email=r["email"] if "email" in keys else None,
            sso_provider_id=r["sso_provider_id"] if "sso_provider_id" in keys else None,
            sso_subject=r["sso_subject"] if "sso_subject" in keys else None,
            permissions=_parse_permissions(r["permissions"] if "permissions" in keys else None),
            role_name=role_name or None,
            avatar_icon=avatar_icon or None,
            last_login_at=(
                int(r["last_login_at"]) if "last_login_at" in keys and r["last_login_at"] else None
            ),
            mobile=r["mobile"] if "mobile" in keys else None,
        )


class UserRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def create(
        self,
        *,
        username: str,
        password_hash: str | None = None,
        role: str,
        display_name: str | None = None,
        locale: str = "zh",
        email: str | None = None,
        sso_provider_id: int | None = None,
        sso_subject: str | None = None,
        permissions: builtins.list[str] | None = None,
        role_name: str | None = None,
        mobile: str | None = None,
    ) -> int:
        perms_json = json.dumps(permissions or [], ensure_ascii=False)
        with self._db.transaction() as conn:
            from octop.infra.db.repos.youli import lock_login_mapping

            lock_login_mapping(conn, self._db.dialect, sso_provider_id, sso_subject, None)
            return insert_returning_id(
                conn,
                "INSERT INTO users(username, password_hash, role, display_name, locale, "
                "email, sso_provider_id, sso_subject, disabled, created_at, permissions, "
                "role_name, mobile) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
                (
                    username,
                    password_hash,
                    role,
                    display_name,
                    locale,
                    email,
                    sso_provider_id,
                    sso_subject,
                    now_ts(),
                    perms_json,
                    role_name,
                    mobile,
                ),
            )

    def get(self, user_id: int) -> UserRow | None:
        with self._db.connect() as conn:
            r = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return UserRow.from_row(r) if r else None

    def get_by_username(self, username: str) -> UserRow | None:
        with self._db.connect() as conn:
            r = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return UserRow.from_row(r) if r else None

    def get_by_sso(self, provider_id: int, subject: str) -> UserRow | None:
        with self._db.connect() as conn:
            r = conn.execute(
                "SELECT u.* FROM users u "
                "INNER JOIN user_sso_identities i ON i.user_id = u.id "
                "WHERE i.provider_id = ? AND i.subject = ?",
                (provider_id, subject),
            ).fetchone()
            if r is not None:
                return UserRow.from_row(r)
            # Legacy single-slot fallback for rows not yet backfilled.
            r = conn.execute(
                "SELECT * FROM users WHERE sso_provider_id = ? AND sso_subject = ?",
                (provider_id, subject),
            ).fetchone()
        if r is None:
            return None
        row = UserRow.from_row(r)
        self.upsert_sso_identity(row.id, provider_id=provider_id, subject=subject)
        return row

    def list_directory_reservations(
        self, provider_id: int, username: str
    ) -> builtins.list[UserRow]:
        """Passwordless accounts reserved for this directory provider.

        A reservation is an admin-created account with no local password and no
        subject link yet. Matching is case-insensitive so ``Alice`` and ``alice``
        name the same login. More than one row means the caller must not guess.
        """
        text = username.strip()
        if not text:
            return []
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT u.* FROM users u "
                "WHERE u.sso_provider_id = ? "
                "AND u.password_hash IS NULL "
                "AND (u.sso_subject IS NULL OR u.sso_subject = '') "
                "AND lower(u.username) = lower(?) "
                "AND NOT EXISTS ("
                "SELECT 1 FROM user_sso_identities i "
                "WHERE i.user_id = u.id AND i.provider_id = ?"
                ")",
                (provider_id, text, provider_id),
            ).fetchall()
        return map_rows(rows, UserRow)

    def list_sso_identities(self, user_id: int) -> builtins.list[UserSsoIdentityRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT i.user_id, i.provider_id, i.subject, i.created_at, p.kind "
                "FROM user_sso_identities i "
                "INNER JOIN sso_providers p ON p.id = i.provider_id "
                "WHERE i.user_id = ? "
                "ORDER BY i.created_at ASC, i.id ASC",
                (user_id,),
            ).fetchall()
        return map_rows(rows, UserSsoIdentityRow)

    def upsert_sso_identity(
        self,
        user_id: int,
        *,
        provider_id: int,
        subject: str,
    ) -> None:
        """Link ``(provider_id, subject)`` to ``user_id`` (one subject per provider)."""
        ts = now_ts()
        with self._db.transaction() as conn:
            from octop.infra.db.repos.youli import lock_login_mapping, lock_user_binding

            provider = conn.execute(
                "SELECT kind FROM sso_providers WHERE id = ?", (provider_id,)
            ).fetchone()
            if provider is not None and provider["kind"] == "feishu":
                lock_user_binding(conn, self._db.dialect, user_id)
            else:
                lock_login_mapping(conn, self._db.dialect, provider_id, subject, user_id)
                if self._db.dialect == "postgresql":
                    conn.execute(
                        "SELECT id FROM users WHERE id = ? FOR UPDATE", (user_id,)
                    ).fetchone()
            previous = conn.execute(
                "SELECT i.subject, p.kind FROM user_sso_identities i JOIN sso_providers p "
                "ON p.id=i.provider_id WHERE i.user_id = ? AND i.provider_id = ?",
                (user_id, provider_id),
            ).fetchone()
            if previous is not None and previous["subject"] != subject:
                if previous["kind"] == "youli":
                    raise ValueError("unlink the existing employee identity before reassignment")
                if previous["kind"] == "feishu":
                    conn.execute(
                        "INSERT INTO youli_binding_generation(user_id,generation) VALUES (?,1) "
                        "ON CONFLICT(user_id) DO UPDATE SET generation=youli_binding_generation.generation+1",
                        (user_id,),
                    )
                conn.execute(
                    "DELETE FROM feishu_kb_state WHERE user_id = ? AND provider_id = ?",
                    (user_id, provider_id),
                )
                conn.execute(
                    "DELETE FROM feishu_kb_grant WHERE user_id = ? AND provider_id = ?",
                    (user_id, provider_id),
                )
            conn.execute(
                "INSERT INTO user_sso_identities(user_id, provider_id, subject, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, provider_id) DO UPDATE SET subject = excluded.subject",
                (user_id, provider_id, subject, ts),
            )
            conn.execute(
                "UPDATE users SET sso_provider_id = ?, sso_subject = ? WHERE id = ?",
                (provider_id, subject, user_id),
            )

    def remove_sso_identity(self, user_id: int, *, provider_id: int) -> bool:
        """Remove one provider link. Returns True when a row was deleted."""
        with self._db.transaction() as conn:
            from octop.infra.db.repos.youli import lock_employee, lock_user_binding, revoke_binding

            provider = conn.execute(
                "SELECT kind FROM sso_providers WHERE id = ?", (provider_id,)
            ).fetchone()
            if provider is not None and provider["kind"] == "youli":
                identity = conn.execute(
                    "SELECT subject FROM user_sso_identities WHERE user_id = ? AND provider_id = ?",
                    (user_id, provider_id),
                ).fetchone()
                if identity:
                    parts = str(identity["subject"]).split(":")
                    if (
                        len(parts) == 3
                        and parts[0] == "youli"
                        and all(p.isdigit() for p in parts[1:])
                    ):
                        lock_employee(conn, self._db.dialect, int(parts[1]), int(parts[2]))
            binding = lock_user_binding(conn, self._db.dialect, user_id)
            if provider is not None and provider["kind"] == "feishu":
                conn.execute(
                    "DELETE FROM feishu_kb_state WHERE user_id = ? AND provider_id = ?",
                    (user_id, provider_id),
                )
                conn.execute(
                    "DELETE FROM feishu_kb_grant WHERE user_id = ? AND provider_id = ?",
                    (user_id, provider_id),
                )
                conn.execute(
                    "INSERT INTO youli_binding_generation(user_id, generation) VALUES (?, 1) "
                    "ON CONFLICT(user_id) DO UPDATE SET generation = youli_binding_generation.generation + 1",
                    (user_id,),
                )
                if binding is not None:
                    revoke_binding(conn, user_id, binding)
            cur = conn.execute(
                "DELETE FROM user_sso_identities WHERE user_id = ? AND provider_id = ?",
                (user_id, provider_id),
            )
            deleted = int(cur.rowcount or 0) > 0
            remaining = conn.execute(
                "SELECT provider_id, subject FROM user_sso_identities "
                "WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            if remaining is None:
                conn.execute(
                    "UPDATE users SET sso_provider_id = NULL, sso_subject = NULL WHERE id = ?",
                    (user_id,),
                )
            else:
                conn.execute(
                    "UPDATE users SET sso_provider_id = ?, sso_subject = ? WHERE id = ?",
                    (remaining["provider_id"], remaining["subject"], user_id),
                )
        return deleted

    def get_by_email(self, email: str) -> UserRow | None:
        with self._db.connect() as conn:
            r = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return UserRow.from_row(r) if r else None

    def get_by_mobile(self, mobile: str) -> UserRow | None:
        with self._db.connect() as conn:
            r = conn.execute("SELECT * FROM users WHERE mobile = ?", (mobile,)).fetchone()
        return UserRow.from_row(r) if r else None

    def set_mobile_if_empty(self, user_id: int, mobile: str) -> None:
        with self._db.transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM users WHERE mobile = ? AND id != ?", (mobile, user_id)
            ).fetchone()
            if existing is not None:
                return
            conn.execute(
                "UPDATE users SET mobile = ? WHERE id = ? AND (mobile IS NULL OR mobile = '')",
                (mobile, user_id),
            )

    def set_sso(
        self,
        user_id: int,
        *,
        sso_provider_id: int | None,
        sso_subject: str | None,
    ) -> None:
        """Legacy primary-slot writer; prefer ``upsert_sso_identity`` / ``remove_sso_identity``."""
        with self._db.transaction() as conn:
            from octop.infra.db.repos.youli import lock_login_mapping

            lock_login_mapping(conn, self._db.dialect, sso_provider_id, sso_subject, user_id)
            if self._db.dialect == "postgresql":
                conn.execute("SELECT id FROM users WHERE id = ? FOR UPDATE", (user_id,)).fetchone()
            current = conn.execute(
                "SELECT p.kind, u.sso_provider_id, u.sso_subject FROM users u "
                "LEFT JOIN sso_providers p ON p.id = u.sso_provider_id WHERE u.id = ?",
                (user_id,),
            ).fetchone()
            if (
                current
                and current["kind"] == "youli"
                and (
                    current["sso_provider_id"] != sso_provider_id
                    or current["sso_subject"] != sso_subject
                )
            ):
                raise ValueError("unlink the existing employee identity before reassignment")
            conn.execute(
                "UPDATE users SET sso_provider_id = ?, sso_subject = ? WHERE id = ?",
                (sso_provider_id, sso_subject, user_id),
            )

    def update_sso_profile(
        self,
        user_id: int,
        *,
        email: str | None = None,
        display_name: str | None = None,
    ) -> None:
        fields: list[str] = []
        params: list[object] = []
        if email is not None:
            fields.append("email = ?")
            params.append(email)
        if display_name is not None:
            fields.append("display_name = ?")
            params.append(display_name)
        if not fields:
            return
        params.append(user_id)
        with self._db.transaction() as conn:
            conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", params)

    def list(self, *, include_disabled: bool = False) -> builtins.list[UserRow]:
        sql = "SELECT * FROM users"
        if not include_disabled:
            sql += " WHERE disabled = 0"
        sql += " ORDER BY username"
        with self._db.connect() as conn:
            rows = conn.execute(sql).fetchall()
        return map_rows(rows, UserRow)

    def set_disabled(self, user_id: int, disabled: bool) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE users SET disabled = ? WHERE id = ?",
                (bool_int(disabled), user_id),
            )

    def set_role(self, user_id: int, role: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))

    def set_password_hash(self, user_id: int, password_hash: str) -> None:
        """Store a new password hash and invalidate outstanding access tokens."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, password_version = password_version + 1 "
                "WHERE id = ?",
                (password_hash, user_id),
            )

    def set_display_name(self, user_id: int, display_name: str | None) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE users SET display_name = ? WHERE id = ?",
                (display_name, user_id),
            )

    def set_email(self, user_id: int, email: str | None) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE users SET email = ? WHERE id = ?", (email, user_id))

    def set_locale(self, user_id: int, locale: str) -> None:
        with self._db.transaction() as conn:
            conn.execute("UPDATE users SET locale = ? WHERE id = ?", (locale, user_id))

    def set_preferences_json(self, user_id: int, preferences_json: str) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE users SET preferences_json = ? WHERE id = ?",
                (preferences_json, user_id),
            )

    def set_role_name(self, user_id: int, role_name: str | None) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE users SET role_name = ? WHERE id = ?",
                (role_name, user_id),
            )

    def set_avatar_icon(self, user_id: int, avatar_icon: str | None) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE users SET avatar_icon = ? WHERE id = ?",
                (avatar_icon, user_id),
            )

    def set_permissions(self, user_id: int, permissions: builtins.list[str]) -> None:
        payload = json.dumps(permissions, ensure_ascii=False)
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE users SET permissions = ? WHERE id = ?",
                (payload, user_id),
            )

    def delete(self, user_id: int) -> None:
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def count(self) -> int:
        with self._db.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def count_by_role(self, role: str) -> int:
        with self._db.connect() as conn:
            return int(
                conn.execute("SELECT COUNT(*) FROM users WHERE role = ?", (role,)).fetchone()[0]
            )

    def clear_login_lockout(self, user_id: int) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE users SET login_failed_count = 0, login_locked_until = 0 WHERE id = ?",
                (user_id,),
            )

    def touch_last_login(self, user_id: int, *, now: int | None = None) -> None:
        """Record a successful sign-in. ``now`` is unix seconds."""
        ts = now if now is not None else now_ts()
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?",
                (ts, user_id),
            )

    def bump_mfa_version(self, user_id: int) -> int:
        """Increment ``mfa_version`` and clear MFA throttle. Returns the new version."""
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE users SET mfa_version = mfa_version + 1, "
                "mfa_failed_count = 0, mfa_locked_until = 0 WHERE id = ?",
                (user_id,),
            )
            row = conn.execute(
                "SELECT mfa_version FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"user {user_id} missing after mfa_version bump")
        return int(row["mfa_version"] or 0)

    def clear_mfa_throttle(self, user_id: int) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                "UPDATE users SET mfa_failed_count = 0, mfa_locked_until = 0 WHERE id = ?",
                (user_id,),
            )

    def record_mfa_failure(
        self,
        user_id: int,
        *,
        max_attempts: int = 5,
        lockout_seconds: int = 900,
        now: int | None = None,
    ) -> int:
        """Increment MFA failure count; lock when threshold reached.

        Does **not** touch ``login_locked_until``. Returns retry_after seconds if locked.
        """
        ts = now if now is not None else now_ts()
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT mfa_failed_count, mfa_locked_until FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                return 0
            locked_until = int(row["mfa_locked_until"] or 0)
            if locked_until > ts:
                return locked_until - ts
            failed = int(row["mfa_failed_count"] or 0) + 1
            new_locked_until = 0
            retry_after = 0
            if failed >= max_attempts:
                new_locked_until = ts + lockout_seconds
                retry_after = lockout_seconds
                failed = 0
            conn.execute(
                "UPDATE users SET mfa_failed_count = ?, mfa_locked_until = ? WHERE id = ?",
                (failed, new_locked_until, user_id),
            )
        return retry_after

    def mfa_retry_after(self, user_id: int, *, now: int | None = None) -> int:
        ts = now if now is not None else now_ts()
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT mfa_locked_until FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return 0
        locked_until = int(row["mfa_locked_until"] or 0)
        return max(0, locked_until - ts)

    def record_failed_login(
        self,
        user_id: int,
        *,
        max_attempts: int,
        lockout_seconds: int,
        now: int | None = None,
    ) -> int:
        """Increment failure count; lock when threshold reached. Returns retry_after seconds if locked."""
        ts = now if now is not None else now_ts()
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT login_failed_count, login_locked_until FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                return 0
            locked_until = int(row["login_locked_until"] or 0)
            if locked_until > ts:
                return locked_until - ts
            failed = int(row["login_failed_count"] or 0) + 1
            new_locked_until = 0
            retry_after = 0
            if failed >= max_attempts:
                new_locked_until = ts + lockout_seconds
                retry_after = lockout_seconds
                failed = 0
            conn.execute(
                "UPDATE users SET login_failed_count = ?, login_locked_until = ? WHERE id = ?",
                (failed, new_locked_until, user_id),
            )
        return retry_after
