"""Control-plane tables and atomic publication of shared Feishu documents."""

from __future__ import annotations

from typing import Any

from octop.infra.db.pool import DatabasePool


def _next_sync(source: Any, now: int, *, is_error: bool = False) -> int:
    if source["schedule"] == "manual":
        return 0
    interval_minutes = int(source["interval_minutes"] or 0)
    if interval_minutes > 0:
        seconds = interval_minutes * 60
        return now + (max(seconds, 300) if is_error else seconds)
    return now + 86400


class SharedFeishuRepo:
    def __init__(self, db: DatabasePool) -> None:
        self.db = db

    def store_state(self, params: tuple[Any, ...], now: int) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM kb_feishu_connection_states WHERE expires_at <= ?", (now,))
            conn.execute(
                "INSERT INTO kb_feishu_connection_states VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                params,
            )

    def consume_state(self, state: str, owner: int, session: str, now: int) -> Any:
        with self.db.transaction() as conn:
            return conn.execute(
                "DELETE FROM kb_feishu_connection_states WHERE state_hash=? AND owner_user_id=? "
                "AND session_hash=? AND expires_at>? RETURNING *",
                (state, owner, session, now),
            ).fetchone()

    def connect(self, params: tuple[Any, ...]) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO kb_feishu_connections "
                "(id,owner_user_id,provider_id,subject,label,token_blob,expires_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(owner_user_id,provider_id,subject) DO UPDATE SET label=excluded.label, "
                "token_blob=excluded.token_blob,expires_at=excluded.expires_at,refreshing=0",
                params,
            )

    def connections(self, owner: int) -> list[Any]:
        with self.db.connect() as conn:
            return list(
                conn.execute(
                    "SELECT id,label,provider_id,subject, token_blob IS NOT NULL AND refreshing=0 AS connected "
                    "FROM kb_feishu_connections WHERE owner_user_id=? ORDER BY label,id",
                    (owner,),
                ).fetchall()
            )

    def connection(self, owner: int, connection_id: str) -> Any:
        with self.db.connect() as conn:
            return conn.execute(
                "SELECT * FROM kb_feishu_connections WHERE owner_user_id=? AND id=?",
                (owner, connection_id),
            ).fetchone()

    def claim_refresh(self, connection_id: str, old: bytes) -> bool:
        with self.db.transaction() as conn:
            return bool(
                conn.execute(
                    "UPDATE kb_feishu_connections SET refreshing=1 WHERE id=? AND refreshing=0 AND token_blob=?",
                    (connection_id, old),
                ).rowcount
                == 1
            )

    def finish_refresh(self, connection_id: str, old: bytes, blob: bytes, expires_at: int) -> bool:
        with self.db.transaction() as conn:
            return bool(
                conn.execute(
                    "UPDATE kb_feishu_connections SET refreshing=0,token_blob=?,expires_at=? "
                    "WHERE id=? AND refreshing=1 AND token_blob=?",
                    (blob, expires_at, connection_id, old),
                ).rowcount
                == 1
            )

    def sources(self, kb_id: str) -> list[Any]:
        with self.db.connect() as conn:
            return list(
                conn.execute(
                    "SELECT s.id,s.connection_id,c.label AS connection_label,c.owner_user_id,s.title,s.scope_kind,"
                    "s.space_id,s.root_token,s.include_children,"
                    "CASE WHEN s.schedule='daily' AND s.interval_minutes>0 THEN 'interval' ELSE s.schedule END AS schedule,"
                    "CASE WHEN s.schedule='daily' AND s.interval_minutes>0 THEN s.interval_minutes ELSE NULL END AS interval_minutes,"
                    "s.auto_add,s.active,s.status,"
                    "s.last_sync,s.last_error,s.pending_count,s.skipped_count,"
                    "COUNT(d.document_id) AS documents FROM kb_feishu_sources s "
                    "JOIN kb_feishu_connections c ON c.id=s.connection_id "
                    "LEFT JOIN knowledge_documents d ON d.feishu_source_id=s.id WHERE s.kb_id=? "
                    "GROUP BY s.id,c.label,c.owner_user_id ORDER BY s.approved_at,s.id",
                    (kb_id,),
                ).fetchall()
            )

    def approved_document_ids(
        self, kb_id: str, connection_id: str, scope_kind: str, space_id: str, root_token: str
    ) -> str | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT approved_document_ids FROM kb_feishu_sources "
                "WHERE kb_id=? AND connection_id=? AND scope_kind=? AND space_id=? AND root_token=?",
                (kb_id, connection_id, scope_kind, space_id, root_token),
            ).fetchone()
            return str(row["approved_document_ids"]) if row is not None else None

    def save_source(self, params: tuple[Any, ...]) -> Any:
        with self.db.transaction() as conn:
            if (
                conn.execute(
                    "UPDATE kb_feishu_connections SET label=label WHERE id=? AND owner_user_id=? "
                    "AND token_blob IS NOT NULL AND refreshing=0",
                    params[-2:],
                ).rowcount
                != 1
            ):
                return None
            return conn.execute(
                "INSERT INTO kb_feishu_sources (id,kb_id,connection_id,approved_by,scope_kind,root_token,"
                "space_id,title,include_children,auto_add,schedule,interval_minutes,approved_at,approved_document_ids,"
                "approval_visibility,next_sync) SELECT ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,? "
                "WHERE EXISTS (SELECT 1 FROM kb_feishu_connections WHERE id=? AND owner_user_id=? "
                "AND token_blob IS NOT NULL AND refreshing=0) "
                "ON CONFLICT(kb_id,connection_id,scope_kind,space_id,root_token) DO UPDATE SET "
                "title=excluded.title,include_children=excluded.include_children,auto_add=excluded.auto_add,"
                "schedule=excluded.schedule,interval_minutes=excluded.interval_minutes,approved_by=excluded.approved_by,approved_at=excluded.approved_at,"
                "approved_document_ids=excluded.approved_document_ids,approval_visibility=excluded.approval_visibility,"
                "active=1,status='queued',pending_count=0,last_error=NULL,next_sync=excluded.next_sync,lease_token=NULL,lease_until=0 "
                "RETURNING id",
                params,
            ).fetchone()

    def set_active(self, kb_id: str, source_id: str, owner: int, active: bool, when: int) -> bool:
        with self.db.transaction() as conn:
            return bool(
                conn.execute(
                    "UPDATE kb_feishu_sources SET active=?,status=?,next_sync=?,lease_token=NULL,lease_until=0 "
                    "WHERE kb_id=? AND id=? AND (?=0 OR lease_until<=?) AND EXISTS (SELECT 1 FROM kb_feishu_connections c "
                    "WHERE c.id=connection_id AND c.owner_user_id=? AND c.token_blob IS NOT NULL)",
                    (
                        int(active),
                        "queued" if active else "paused",
                        when,
                        kb_id,
                        source_id,
                        int(active),
                        when,
                        owner,
                    ),
                ).rowcount
                == 1
            )

    def due(self, now: int, limit: int) -> list[Any]:
        with self.db.connect() as conn:
            return list(
                conn.execute(
                    "SELECT s.id FROM kb_feishu_sources s JOIN kb_feishu_connections c ON c.id=s.connection_id "
                    "WHERE s.active=1 AND s.next_sync>0 AND s.next_sync<=? AND s.lease_until<=? "
                    "AND c.token_blob IS NOT NULL ORDER BY s.next_sync,s.id LIMIT ?",
                    (now, now, limit),
                ).fetchall()
            )

    def claim(self, source_id: str, token: str, now: int, deadline: int) -> Any:
        with self.db.transaction() as conn:
            return conn.execute(
                "UPDATE kb_feishu_sources SET status='syncing',lease_token=?,lease_until=? "
                "WHERE id=? AND active=1 AND next_sync>0 AND next_sync<=? AND lease_until<=? RETURNING *",
                (token, deadline, source_id, now, now),
            ).fetchone()

    def published(self, source_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            return {
                row["feishu_document_id"]: row
                for row in conn.execute(
                    "SELECT document_id,filename,content_hash,feishu_document_id,feishu_origin "
                    "FROM knowledge_documents WHERE feishu_source_id=?",
                    (source_id,),
                ).fetchall()
            }

    def retain_blob(self, doc_id: str, kb_id: str, filename: str, deadline: int) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO kb_feishu_blob_cleanup VALUES (?,?,?,?)",
                (doc_id, kb_id, filename, deadline),
            )

    @staticmethod
    def _remove(conn: Any, kb_id: str, doc_id: str, filename: str, now: int) -> int:
        removed = conn.execute(
            "DELETE FROM knowledge_documents WHERE kb_id=? AND document_id=?", (kb_id, doc_id)
        ).rowcount
        if removed:
            conn.execute(
                "INSERT INTO kb_feishu_blob_cleanup VALUES (?,?,?,?) ON CONFLICT(document_id) DO NOTHING",
                (doc_id, kb_id, filename, now),
            )
        return int(removed)

    def publish(
        self,
        *,
        source: Any,
        token: str,
        now: int,
        insert: list[tuple[Any, ...]],
        keep_ids: set[str],
        pending: int,
        skipped: int,
        approved_document_ids: str,
    ) -> bool:
        with self.db.transaction() as conn:
            if (
                conn.execute(
                    "UPDATE kb_feishu_sources SET status='synced',last_sync=?,last_error=NULL,pending_count=?,"
                    "skipped_count=?,approved_document_ids=?,next_sync=?,lease_token=NULL,lease_until=0 "
                    "WHERE id=? AND lease_token=? AND lease_until>? AND active=1",
                    (
                        now,
                        pending,
                        skipped,
                        approved_document_ids,
                        _next_sync(source, now),
                        source["id"],
                        token,
                        now,
                    ),
                ).rowcount
                != 1
            ):
                return False
            kb_id = source["kb_id"]
            removed = 0
            for existing in conn.execute(
                "SELECT document_id,filename FROM knowledge_documents WHERE feishu_source_id=?",
                (source["id"],),
            ).fetchall():
                if existing["document_id"] not in keep_ids:
                    removed += self._remove(
                        conn, kb_id, existing["document_id"], existing["filename"], now
                    )
            difference = len(insert) - removed
            if (
                conn.execute(
                    "UPDATE knowledge_bases SET doc_count=doc_count+?,updated_at=? WHERE knowledge_base_id=? "
                    "AND (max_documents=0 OR doc_count+?<=max_documents OR ?<=0)",
                    (difference, now, kb_id, difference, difference),
                ).rowcount
                != 1
            ):
                raise ValueError("document_limit")
            for row in insert:
                conn.execute(
                    "INSERT INTO knowledge_documents (document_id,kb_id,path,filename,is_dir,content_type,"
                    "byte_size,content_hash,status,error_message,chunk_count,created_at,updated_at,"
                    "feishu_source_id,feishu_document_id,feishu_origin) "
                    "VALUES (?,?,?,?,0,'text/plain',?,?,'pending','',0,?,?,?,?,?)",
                    row,
                )
                conn.execute("DELETE FROM kb_feishu_blob_cleanup WHERE document_id=?", (row[0],))
            return True

    def fail(self, source: Any, token: str, now: int, reason: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE kb_feishu_sources SET status='error',last_error=?,next_sync=?,lease_token=NULL,lease_until=0 "
                "WHERE id=? AND lease_token=?",
                (reason, _next_sync(source, now, is_error=True), source["id"], token),
            )

    def disconnect(self, owner: int, connection_id: str, now: int) -> bool:
        with self.db.transaction() as conn:
            if (
                conn.execute(
                    "UPDATE kb_feishu_connections SET token_blob=NULL,refreshing=0,expires_at=0 WHERE owner_user_id=? AND id=?",
                    (owner, connection_id),
                ).rowcount
                != 1
            ):
                return False
            conn.execute(
                "UPDATE kb_feishu_sources SET active=0,status='disconnected',lease_token=NULL,lease_until=0 "
                "WHERE connection_id=?",
                (connection_id,),
            )
            rows = conn.execute(
                "SELECT d.document_id,d.kb_id,d.filename FROM knowledge_documents d JOIN kb_feishu_sources s "
                "ON d.feishu_source_id=s.id WHERE s.connection_id=?",
                (connection_id,),
            ).fetchall()
            counts: dict[str, int] = {}
            for row in rows:
                kb_id = row["kb_id"]
                counts[kb_id] = counts.get(kb_id, 0) + self._remove(
                    conn, kb_id, row["document_id"], row["filename"], now
                )
            for kb_id, count in counts.items():
                conn.execute(
                    "UPDATE knowledge_bases SET doc_count=doc_count-?,updated_at=? WHERE knowledge_base_id=?",
                    (count, now, kb_id),
                )
            return True

    def cleanup(self, now: int, limit: int) -> list[Any]:
        with self.db.connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM kb_feishu_blob_cleanup WHERE available_at<=? ORDER BY available_at,document_id LIMIT ?",
                    (now, limit),
                ).fetchall()
            )

    def finish_cleanup(self, doc_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM kb_feishu_blob_cleanup WHERE document_id=?", (doc_id,))
