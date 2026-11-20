"""Persistence for owner-only Feishu document staging."""

from __future__ import annotations

from typing import Any

from octop.infra.db.pool import DatabasePool


class FeishuKbRepo:
    def __init__(self, db: DatabasePool) -> None:
        self.db = db

    def linked_subject(self, user: int, provider: int) -> str | None:
        with self.db.connect() as c:
            row = c.execute(
                "SELECT i.subject FROM user_sso_identities i JOIN sso_providers p ON p.id=i.provider_id JOIN users u ON u.id=i.user_id "
                "WHERE i.user_id=? AND i.provider_id=? AND p.kind='feishu' AND p.enabled=1 AND u.disabled=0",
                (user, provider),
            ).fetchone()
            return str(row["subject"]) if row else None

    def identities(self, user: int) -> list[Any]:
        with self.db.connect() as c:
            return list(
                c.execute(
                    "SELECT i.provider_id,i.subject FROM user_sso_identities i "
                    "JOIN sso_providers p ON p.id=i.provider_id JOIN users u ON u.id=i.user_id "
                    "WHERE i.user_id=? AND p.kind='feishu' AND p.enabled=1 AND u.disabled=0",
                    (user,),
                ).fetchall()
            )

    def state_create(self, values: tuple[Any, ...], now: int) -> bool:
        with self.db.transaction() as c:
            if self.db.dialect == "postgresql":
                c.execute("SELECT id FROM users WHERE id=? FOR UPDATE", (values[1],)).fetchone()
            valid = c.execute(
                "SELECT 1 FROM user_sso_identities i JOIN users u ON u.id=i.user_id "
                "JOIN sso_providers p ON p.id=i.provider_id WHERE i.user_id=? "
                "AND i.provider_id=? AND i.subject=? AND u.disabled=0 "
                "AND p.enabled=1 AND p.kind='feishu'",
                (values[1], values[2], values[6]),
            ).fetchone()
            if not valid:
                return False
            c.execute("DELETE FROM feishu_kb_state WHERE expires_at<=?", (now,))
            c.execute(
                "INSERT INTO feishu_kb_state(state_hash,user_id,provider_id,session_hash,verifier_blob,redirect_uri,subject,expires_at) VALUES (?,?,?,?,?,?,?,?)",
                values,
            )
            return True

    def state_consume(self, state: str, user: int, session: str, now: int) -> Any | None:
        with self.db.transaction() as c:
            cur = c.execute(
                "UPDATE feishu_kb_state SET consumed=1 WHERE state_hash=? AND user_id=? AND session_hash=? AND expires_at>? AND consumed=0",
                (state, user, session, now),
            )
            if cur.rowcount != 1:
                return None
            return c.execute(
                "SELECT * FROM feishu_kb_state WHERE state_hash=?", (state,)
            ).fetchone()

    def grant_finish(
        self,
        state: str,
        user: int,
        provider: int,
        subject: str,
        blob: bytes,
        expires: int,
        now: int,
    ) -> bool:
        with self.db.transaction() as c:
            if self.db.dialect == "postgresql":
                c.execute("SELECT id FROM users WHERE id=? FOR UPDATE", (user,)).fetchone()
            valid = c.execute(
                "SELECT 1 FROM feishu_kb_state s JOIN user_sso_identities i ON i.user_id=s.user_id AND i.provider_id=s.provider_id AND i.subject=s.subject JOIN sso_providers p ON p.id=i.provider_id WHERE s.state_hash=? AND s.user_id=? AND s.provider_id=? AND s.subject=? AND s.consumed=1 AND s.expires_at>? AND p.enabled=1 AND p.kind='feishu'",
                (state, user, provider, subject, now),
            ).fetchone()
            if not valid:
                return False
            c.execute("DELETE FROM feishu_kb_state WHERE state_hash=?", (state,))
            c.execute(
                "INSERT INTO feishu_kb_grant(user_id,provider_id,subject,token_blob,expires_at,refreshing) VALUES (?,?,?,?,?,0) ON CONFLICT(user_id,provider_id) DO UPDATE SET subject=excluded.subject,token_blob=excluded.token_blob,expires_at=excluded.expires_at,refreshing=0",
                (user, provider, subject, blob, expires),
            )
            return True

    def grant(self, user: int, provider: int) -> Any | None:
        with self.db.connect() as c:
            return c.execute(
                "SELECT g.* FROM feishu_kb_grant g JOIN user_sso_identities i ON i.user_id=g.user_id AND i.provider_id=g.provider_id AND i.subject=g.subject JOIN sso_providers p ON p.id=g.provider_id JOIN users u ON u.id=g.user_id WHERE g.user_id=? AND g.provider_id=? AND p.kind='feishu' AND p.enabled=1 AND u.disabled=0",
                (user, provider),
            ).fetchone()

    def claim_refresh(self, user: int, provider: int, blob: bytes) -> bool:
        with self.db.transaction() as c:
            return (
                int(
                    c.execute(
                        "UPDATE feishu_kb_grant SET refreshing=1 WHERE user_id=? AND provider_id=? AND token_blob=? AND refreshing=0",
                        (user, provider, blob),
                    ).rowcount
                )
                == 1
            )

    def finish_refresh(
        self, user: int, provider: int, old: bytes, blob: bytes, expires: int
    ) -> bool:
        with self.db.transaction() as c:
            return (
                int(
                    c.execute(
                        "UPDATE feishu_kb_grant SET token_blob=?,expires_at=?,refreshing=0 WHERE user_id=? AND provider_id=? AND token_blob=? AND refreshing=1",
                        (blob, expires, user, provider, old),
                    ).rowcount
                )
                == 1
            )

    def revoke(self, user: int, provider: int) -> None:
        with self.db.transaction() as c:
            if self.db.dialect == "postgresql":
                c.execute("SELECT id FROM users WHERE id=? FOR UPDATE", (user,)).fetchone()
            c.execute(
                "DELETE FROM feishu_kb_state WHERE user_id=? AND provider_id=?", (user, provider)
            )
            c.execute(
                "DELETE FROM feishu_kb_grant WHERE user_id=? AND provider_id=?", (user, provider)
            )

    def create_source(self, source: str, user: int, provider: int, document: str) -> bool:
        with self.db.transaction() as c:
            if self.db.dialect == "postgresql":
                c.execute("SELECT id FROM users WHERE id=? FOR UPDATE", (user,)).fetchone()
            count = c.execute(
                "SELECT COUNT(*) AS n FROM feishu_kb_source WHERE user_id=?", (user,)
            ).fetchone()
            if int(count["n"]) >= 100:
                return False
            c.execute(
                "INSERT INTO feishu_kb_source(source_id,user_id,provider_id,document_id) VALUES (?,?,?,?)",
                (source, user, provider, document),
            )
            return True

    def configure(
        self, source: str, user: int, enabled: bool, interval: int, retrieval: bool | None = None
    ) -> bool:
        with self.db.transaction() as c:
            return (
                int(
                    c.execute(
                        "UPDATE feishu_kb_source SET enabled=?,interval_seconds=?,retrieval_enabled=COALESCE(?,retrieval_enabled),retrieval_generation=retrieval_generation+1,lease_token=NULL,lease_until=0,content=CASE WHEN ?=0 THEN NULL ELSE content END,digest=CASE WHEN ?=0 THEN NULL ELSE digest END WHERE source_id=? AND user_id=?",
                        (
                            int(enabled),
                            interval,
                            None if retrieval is None else int(retrieval),
                            int(enabled),
                            int(enabled),
                            source,
                            user,
                        ),
                    ).rowcount
                )
                == 1
            )

    def sources(self, user: int) -> list[Any]:
        with self.db.connect() as c:
            return list(
                c.execute(
                    "SELECT source_id,provider_id,document_id,enabled,retrieval_enabled,interval_seconds,next_sync,status,digest FROM feishu_kb_source WHERE user_id=? ORDER BY source_id LIMIT 200",
                    (user,),
                ).fetchall()
            )

    def source(self, source: str, user: int) -> Any | None:
        with self.db.connect() as c:
            return c.execute(
                "SELECT * FROM feishu_kb_source WHERE source_id=? AND user_id=?", (source, user)
            ).fetchone()

    def delete_source(self, source: str, user: int) -> bool:
        with self.db.transaction() as c:
            return (
                int(
                    c.execute(
                        "DELETE FROM feishu_kb_source WHERE source_id=? AND user_id=?",
                        (source, user),
                    ).rowcount
                )
                == 1
            )

    def due(self, now: int, limit: int) -> list[Any]:
        with self.db.connect() as c:
            return list(
                c.execute(
                    "SELECT source_id,user_id FROM feishu_kb_source WHERE enabled=1 AND next_sync<=? AND lease_until<=? ORDER BY next_sync LIMIT ?",
                    (now, now, limit),
                ).fetchall()
            )

    def claim(self, source: str, user: int, token: str, now: int) -> bool:
        with self.db.transaction() as c:
            return (
                int(
                    c.execute(
                        "UPDATE feishu_kb_source SET lease_token=?,lease_until=? WHERE source_id=? AND user_id=? AND enabled=1 AND lease_until<=?",
                        (token, now + 300, source, user, now),
                    ).rowcount
                )
                == 1
            )

    def finish(
        self,
        source: str,
        user: int,
        token: str,
        now: int,
        status: str,
        digest: str | None,
        content: str | None,
    ) -> bool:
        with self.db.transaction() as c:
            return (
                int(
                    c.execute(
                        "UPDATE feishu_kb_source SET status=?,retrieval_generation=retrieval_generation+CASE WHEN digest IS NULL OR digest<>? THEN 1 ELSE 0 END,digest=?,content=?,next_sync=?+interval_seconds,lease_token=NULL,lease_until=0 WHERE source_id=? AND user_id=? AND enabled=1 AND lease_token=? AND lease_until>? AND EXISTS (SELECT 1 FROM feishu_kb_grant g JOIN user_sso_identities i ON i.user_id=g.user_id AND i.provider_id=g.provider_id AND i.subject=g.subject WHERE g.user_id=feishu_kb_source.user_id AND g.provider_id=feishu_kb_source.provider_id AND g.refreshing=0)",
                        (status, digest, digest, content, now, source, user, token, now),
                    ).rowcount
                )
                == 1
            )

    def fail(self, source: str, user: int, token: str, now: int, status: str) -> None:
        with self.db.transaction() as c:
            c.execute(
                "UPDATE feishu_kb_source SET status=?,digest=NULL,content=NULL,"
                "next_sync=?+interval_seconds,lease_token=NULL,lease_until=0 "
                "WHERE source_id=? AND user_id=? AND lease_token=?",
                (status, now, source, user, token),
            )
