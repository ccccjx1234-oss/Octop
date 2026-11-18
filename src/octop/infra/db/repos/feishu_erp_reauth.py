"""Transactional one-shot Feishu reauthentication for ERP delegation."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from octop.infra.db.pool import DatabasePool


def config_fingerprint(provider: Any) -> str:
    """Fingerprint the provider configuration used for this verification."""
    secret = provider["client_secret_enc"]
    extra = provider["extra"]
    if isinstance(extra, str):
        extra = json.loads(extra)
    data = [
        provider["client_id"],
        bytes(secret).hex() if secret else "",
        provider["issuer"],
        provider["scopes"],
        provider["dashboard_origin"],
        extra,
    ]
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class FeishuErpReauthRepo:
    def __init__(self, db: DatabasePool) -> None:
        self.db = db

    def _lock(self, conn: Any, user: int, provider: int) -> None:
        if self.db.dialect == "postgresql":
            conn.execute("SELECT id FROM users WHERE id=? FOR UPDATE", (user,)).fetchone()
            conn.execute(
                "SELECT id FROM sso_providers WHERE id=? FOR UPDATE", (provider,)
            ).fetchone()

    @staticmethod
    def _valid(conn: Any, row: Any) -> bool:
        identity = conn.execute(
            "SELECT p.client_id, p.client_secret_enc, p.issuer, p.scopes, p.dashboard_origin, p.extra FROM user_sso_identities i "
            "JOIN users u ON u.id=i.user_id JOIN sso_providers p ON p.id=i.provider_id "
            "JOIN youli_binding_generation g ON g.user_id=i.user_id "
            "WHERE i.user_id=? AND i.provider_id=? AND i.subject=? AND u.disabled=0 "
            "AND p.enabled=1 AND p.kind='feishu' AND g.generation=?",
            (row["user_id"], row["provider_id"], row["subject"], row["generation"]),
        ).fetchone()
        if identity is None or identity["client_id"] != row["client_id"]:
            return False
        extra = identity["extra"]
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except ValueError:
                return False
        region = "lark" if isinstance(extra, dict) and extra.get("region") == "lark" else "feishu"
        try:
            return bool(
                region == row["region"]
                and config_fingerprint(identity) == row["config_fingerprint"]
            )
        except (ValueError, TypeError):
            return False

    def create(self, row: dict[str, Any], now: int) -> bool:
        with self.db.transaction() as conn:
            self._lock(conn, row["user_id"], row["provider_id"])
            conn.execute(
                "INSERT INTO youli_binding_generation(user_id,generation) VALUES (?,0) ON CONFLICT(user_id) DO NOTHING",
                (row["user_id"],),
            )
            generation = conn.execute(
                "SELECT generation FROM youli_binding_generation WHERE user_id=?", (row["user_id"],)
            ).fetchone()
            row["generation"] = int(generation["generation"])
            if not self._valid(conn, row):
                return False
            conn.execute("DELETE FROM feishu_erp_reauth_state WHERE expires_at<=?", (now,))
            conn.execute("DELETE FROM feishu_erp_reauth_proof WHERE expires_at<=?", (now,))
            conn.execute(
                "INSERT INTO feishu_erp_reauth_state(state_hash,user_id,provider_id,session_hash,subject,client_id,region,config_fingerprint,verifier_blob,redirect_uri,generation,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(
                    row[k]
                    for k in (
                        "state_hash",
                        "user_id",
                        "provider_id",
                        "session_hash",
                        "subject",
                        "client_id",
                        "region",
                        "config_fingerprint",
                        "verifier_blob",
                        "redirect_uri",
                        "generation",
                        "expires_at",
                    )
                ),
            )
            return True

    def consume_state(self, state: str, user: int, session: str, now: int) -> Any | None:
        with self.db.transaction() as conn:
            changed = conn.execute(
                "UPDATE feishu_erp_reauth_state SET consumed=1 WHERE state_hash=? AND user_id=? AND session_hash=? AND expires_at>? AND consumed=0",
                (state, user, session, now),
            )
            if changed.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM feishu_erp_reauth_state WHERE state_hash=?", (state,)
            ).fetchone()
            return row

    def finish(self, state: str, user: int, provider: int, proof: str, now: int) -> bool:
        with self.db.transaction() as conn:
            self._lock(conn, user, provider)
            row = conn.execute(
                "SELECT * FROM feishu_erp_reauth_state WHERE state_hash=? AND user_id=? AND provider_id=? AND consumed=1 AND expires_at>?",
                (state, user, provider, now),
            ).fetchone()
            if row is None or not self._valid(conn, row):
                return False
            conn.execute("DELETE FROM feishu_erp_reauth_state WHERE state_hash=?", (state,))
            conn.execute(
                "INSERT INTO feishu_erp_reauth_proof(proof_hash,user_id,provider_id,session_hash,subject,client_id,region,config_fingerprint,generation,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    proof,
                    user,
                    provider,
                    row["session_hash"],
                    row["subject"],
                    row["client_id"],
                    row["region"],
                    row["config_fingerprint"],
                    row["generation"],
                    now + 120,
                ),
            )
            return True

    def claim(self, proof: str, user: int, session: str, now: int) -> Any | None:
        with self.db.transaction() as conn:
            # Lock the user before selecting the proof to serialize unlink and other claims.
            if self.db.dialect == "postgresql":
                conn.execute("SELECT id FROM users WHERE id=? FOR UPDATE", (user,)).fetchone()
            row = conn.execute(
                "SELECT * FROM feishu_erp_reauth_proof WHERE proof_hash=? AND user_id=? AND session_hash=? AND expires_at>?",
                (proof, user, session, now),
            ).fetchone()
            if row is None:
                return None
            self._lock(conn, user, int(row["provider_id"]))
            conn.execute("DELETE FROM feishu_erp_reauth_proof WHERE proof_hash=?", (proof,))
            if not self._valid(conn, row):
                return None
            updated = conn.execute(
                "UPDATE youli_binding_generation SET generation=generation+1 WHERE user_id=? AND generation=?",
                (user, row["generation"]),
            )
            if updated.rowcount != 1:
                return None
            return dict(row) | {"generation": int(row["generation"]) + 1}
