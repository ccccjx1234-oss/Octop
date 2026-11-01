"""Thumbs up / down on assistant messages (one vote per user per message)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, now_ts


@dataclass(frozen=True)
class FeedbackRow:
    id: int
    agent_id: str
    thread_id: str
    message_id: str
    user_id: int
    rating: int
    comment: str
    excerpt: str
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> FeedbackRow:
        return cls(
            id=int(row["id"]),
            agent_id=str(row["agent_id"]),
            thread_id=str(row["thread_id"]),
            message_id=str(row["message_id"]),
            user_id=int(row["user_id"]),
            rating=int(row["rating"]),
            comment=str(row["comment"] or ""),
            excerpt=str(row["excerpt"] or ""),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )


class MessageFeedbackRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def upsert(
        self,
        *,
        agent_id: str,
        thread_id: str,
        message_id: str,
        user_id: int,
        rating: int,
        comment: str,
        excerpt: str,
    ) -> None:
        ts = now_ts()
        with self._db.connect() as conn:
            conn.execute(
                """
                INSERT INTO message_feedback (
                    agent_id, thread_id, message_id, user_id, rating, comment, excerpt,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (user_id, agent_id, thread_id, message_id) DO UPDATE SET
                    rating = excluded.rating,
                    comment = excluded.comment,
                    excerpt = excluded.excerpt,
                    updated_at = excluded.updated_at
                """,
                (agent_id, thread_id, message_id, user_id, rating, comment, excerpt, ts, ts),
            )

    def delete(self, *, agent_id: str, thread_id: str, message_id: str, user_id: int) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "DELETE FROM message_feedback WHERE agent_id = ? AND thread_id = ?"
                " AND message_id = ? AND user_id = ?",
                (agent_id, thread_id, message_id, user_id),
            )

    def delete_for_thread(self, thread_id: str) -> None:
        with self._db.connect() as conn:
            conn.execute("DELETE FROM message_feedback WHERE thread_id = ?", (thread_id,))

    def ratings_for_thread(self, *, agent_id: str, thread_id: str, user_id: int) -> dict[str, int]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT message_id, rating FROM message_feedback"
                " WHERE agent_id = ? AND thread_id = ? AND user_id = ?",
                (agent_id, thread_id, user_id),
            ).fetchall()
        return {str(row["message_id"]): int(row["rating"]) for row in rows}

    def counts_by_agent(self, *, since: int) -> list[dict[str, Any]]:
        with self._db.connect() as conn:
            rows = conn.execute(
                """
                SELECT agent_id,
                       SUM(CASE WHEN rating > 0 THEN 1 ELSE 0 END) AS up,
                       SUM(CASE WHEN rating < 0 THEN 1 ELSE 0 END) AS down
                FROM message_feedback WHERE updated_at >= ?
                GROUP BY agent_id ORDER BY agent_id
                """,
                (since,),
            ).fetchall()
        return [
            {"agent_id": str(r["agent_id"]), "up": int(r["up"] or 0), "down": int(r["down"] or 0)}
            for r in rows
        ]

    def recent(
        self, *, since: int, rating: int | None = None, limit: int = 100
    ) -> list[FeedbackRow]:
        where = "updated_at >= ?"
        params: list[Any] = [since]
        if rating is not None:
            where += " AND rating = ?"
            params.append(rating)
        params.append(limit)
        with self._db.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM message_feedback WHERE {where} ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return map_rows(rows, FeedbackRow)
