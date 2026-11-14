"""Persistence repository for business workbench tasks, deliverables, and goals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, insert_returning_id, map_rows, now_ts


@dataclass(frozen=True)
class WorkbenchTaskRow:
    id: int
    user_id: int
    role_key: str
    module_key: str
    module_title: str
    target_type: str
    target_name: str
    title: str
    due_label: str
    status: str
    progress: int
    source: str
    external_ref: str
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> WorkbenchTaskRow:
        return cls(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            role_key=str(row["role_key"]),
            module_key=str(row["module_key"]),
            module_title=str(row["module_title"]),
            target_type=str(row["target_type"] or ""),
            target_name=str(row["target_name"] or ""),
            title=str(row["title"]),
            due_label=str(row["due_label"] or ""),
            status=str(row["status"] or "pending"),
            progress=int(row["progress"] or 0),
            source=str(row["source"] or "manual"),
            external_ref=str(row["external_ref"] or ""),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )


@dataclass(frozen=True)
class WorkbenchDeliverableRow:
    id: int
    user_id: int
    role_key: str
    deliverable_type: str
    target_name: str
    title: str
    summary: str
    content: str
    review_status: str
    source_thread_id: str
    source_message_id: str
    linked_task_id: int | None
    delivered_at: int
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> WorkbenchDeliverableRow:
        linked = row["linked_task_id"]
        return cls(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            role_key=str(row["role_key"] or "customer_service"),
            deliverable_type=str(row["deliverable_type"] or "document"),
            target_name=str(row["target_name"] or ""),
            title=str(row["title"]),
            summary=str(row["summary"] or ""),
            content=str(row["content"] or ""),
            review_status=str(row["review_status"] or "pending_review"),
            source_thread_id=str(row["source_thread_id"] or ""),
            source_message_id=str(row["source_message_id"] or ""),
            linked_task_id=int(linked) if linked is not None else None,
            delivered_at=int(row["delivered_at"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )


@dataclass(frozen=True)
class WorkbenchGoalRow:
    id: int
    user_id: int
    role_key: str
    title: str
    target_value: float
    current_value: float
    unit: str
    period: str
    sort_order: int
    updated_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> WorkbenchGoalRow:
        return cls(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            role_key=str(row["role_key"]),
            title=str(row["title"]),
            target_value=float(row["target_value"] or 0),
            current_value=float(row["current_value"] or 0),
            unit=str(row["unit"] or ""),
            period=str(row["period"] or "month"),
            sort_order=int(row["sort_order"] or 0),
            updated_at=int(row["updated_at"]),
        )


class WorkbenchRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def list_tasks(
        self,
        user_id: int,
        role_key: str | None = None,
        *,
        include_seed: bool = False,
    ) -> list[WorkbenchTaskRow]:
        seed_clause = "" if include_seed else " AND source != 'seed'"
        with self._db.connect() as conn:
            if role_key:
                rows = conn.execute(
                    "SELECT * FROM workbench_tasks WHERE user_id = ? AND role_key = ?"
                    f"{seed_clause} ORDER BY id ASC",
                    (user_id, role_key),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM workbench_tasks WHERE user_id = ?{seed_clause} "
                    "ORDER BY updated_at DESC, id ASC",
                    (user_id,),
                ).fetchall()
        return map_rows(rows, WorkbenchTaskRow)

    def purge_seed_tasks(self, user_id: int) -> int:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM workbench_tasks WHERE user_id = ? AND source = 'seed'",
                (user_id,),
            )
            return int(getattr(cur, "rowcount", 0) or 0)

    def get_task(self, user_id: int, task_id: int) -> WorkbenchTaskRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM workbench_tasks WHERE id = ? AND user_id = ?",
                (task_id, user_id),
            ).fetchone()
        return WorkbenchTaskRow.from_row(row) if row else None

    def find_task_by_external_ref(self, user_id: int, external_ref: str) -> WorkbenchTaskRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM workbench_tasks WHERE user_id = ? AND external_ref = ?",
                (user_id, external_ref),
            ).fetchone()
        return WorkbenchTaskRow.from_row(row) if row else None

    def create_task(
        self,
        *,
        user_id: int,
        role_key: str,
        module_key: str,
        module_title: str,
        target_type: str,
        target_name: str,
        title: str,
        due_label: str,
        status: str,
        progress: int,
        source: str = "manual",
        external_ref: str = "",
    ) -> WorkbenchTaskRow:
        ts = now_ts()
        with self._db.transaction() as conn:
            task_id = insert_returning_id(
                conn,
                "INSERT INTO workbench_tasks("
                "user_id, role_key, module_key, module_title, target_type, target_name, "
                "title, due_label, status, progress, source, external_ref, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    role_key,
                    module_key,
                    module_title,
                    target_type,
                    target_name,
                    title,
                    due_label,
                    status,
                    progress,
                    source,
                    external_ref,
                    ts,
                    ts,
                ),
            )
            row = conn.execute("SELECT * FROM workbench_tasks WHERE id = ?", (task_id,)).fetchone()
        assert row is not None
        return WorkbenchTaskRow.from_row(row)

    def update_task(
        self,
        user_id: int,
        task_id: int,
        *,
        title: str | None = None,
        target_name: str | None = None,
        due_label: str | None = None,
        status: str | None = None,
        progress: int | None = None,
    ) -> WorkbenchTaskRow | None:
        fields: list[str] = []
        params: list[Any] = []
        if title is not None:
            fields.append("title = ?")
            params.append(title)
        if target_name is not None:
            fields.append("target_name = ?")
            params.append(target_name)
        if due_label is not None:
            fields.append("due_label = ?")
            params.append(due_label)
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if progress is not None:
            fields.append("progress = ?")
            params.append(progress)
        if not fields:
            return self.get_task(user_id, task_id)
        fields.append("updated_at = ?")
        params.append(now_ts())
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE workbench_tasks SET {', '.join(fields)} WHERE id = ? AND user_id = ?",
                (*params, task_id, user_id),
            )
            row = conn.execute(
                "SELECT * FROM workbench_tasks WHERE id = ? AND user_id = ?",
                (task_id, user_id),
            ).fetchone()
        return WorkbenchTaskRow.from_row(row) if row else None

    def delete_task(self, user_id: int, task_id: int) -> bool:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM workbench_tasks WHERE id = ? AND user_id = ?",
                (task_id, user_id),
            )
            return int(getattr(cur, "rowcount", 0) or 0) > 0

    def list_deliverables(self, user_id: int) -> list[WorkbenchDeliverableRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM workbench_deliverables WHERE user_id = ? "
                "ORDER BY delivered_at DESC, id DESC",
                (user_id,),
            ).fetchall()
        return map_rows(rows, WorkbenchDeliverableRow)

    def get_deliverable(self, user_id: int, deliverable_id: int) -> WorkbenchDeliverableRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM workbench_deliverables WHERE id = ? AND user_id = ?",
                (deliverable_id, user_id),
            ).fetchone()
        return WorkbenchDeliverableRow.from_row(row) if row else None

    def find_deliverable_by_source_message(
        self, user_id: int, source_message_id: str
    ) -> WorkbenchDeliverableRow | None:
        msg_id = source_message_id.strip()
        if not msg_id:
            return None
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM workbench_deliverables "
                "WHERE user_id = ? AND source_message_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (user_id, msg_id),
            ).fetchone()
        return WorkbenchDeliverableRow.from_row(row) if row else None

    def create_deliverable(
        self,
        *,
        user_id: int,
        role_key: str,
        deliverable_type: str,
        target_name: str,
        title: str,
        summary: str,
        content: str,
        review_status: str,
        source_thread_id: str = "",
        source_message_id: str = "",
        linked_task_id: int | None = None,
        delivered_at: int | None = None,
    ) -> WorkbenchDeliverableRow:
        ts = now_ts()
        delivered = delivered_at if delivered_at is not None else ts
        with self._db.transaction() as conn:
            deliv_id = insert_returning_id(
                conn,
                "INSERT INTO workbench_deliverables("
                "user_id, role_key, deliverable_type, target_name, title, summary, content, "
                "review_status, source_thread_id, source_message_id, linked_task_id, "
                "delivered_at, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    role_key,
                    deliverable_type,
                    target_name,
                    title,
                    summary,
                    content,
                    review_status,
                    source_thread_id,
                    source_message_id,
                    linked_task_id,
                    delivered,
                    ts,
                    ts,
                ),
            )
            row = conn.execute(
                "SELECT * FROM workbench_deliverables WHERE id = ?", (deliv_id,)
            ).fetchone()
        assert row is not None
        return WorkbenchDeliverableRow.from_row(row)

    def update_deliverable(
        self,
        user_id: int,
        deliverable_id: int,
        *,
        deliverable_type: str | None = None,
        target_name: str | None = None,
        title: str | None = None,
        summary: str | None = None,
        content: str | None = None,
        review_status: str | None = None,
    ) -> WorkbenchDeliverableRow | None:
        fields: list[str] = []
        params: list[Any] = []
        for col, val in (
            ("deliverable_type", deliverable_type),
            ("target_name", target_name),
            ("title", title),
            ("summary", summary),
            ("content", content),
            ("review_status", review_status),
        ):
            if val is not None:
                fields.append(f"{col} = ?")
                params.append(val)
        if not fields:
            return self.get_deliverable(user_id, deliverable_id)
        fields.append("updated_at = ?")
        params.append(now_ts())
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE workbench_deliverables SET {', '.join(fields)} "
                "WHERE id = ? AND user_id = ?",
                (*params, deliverable_id, user_id),
            )
            row = conn.execute(
                "SELECT * FROM workbench_deliverables WHERE id = ? AND user_id = ?",
                (deliverable_id, user_id),
            ).fetchone()
        return WorkbenchDeliverableRow.from_row(row) if row else None

    def delete_deliverable(self, user_id: int, deliverable_id: int) -> bool:
        with self._db.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM workbench_deliverables WHERE id = ? AND user_id = ?",
                (deliverable_id, user_id),
            )
            return int(getattr(cur, "rowcount", 0) or 0) > 0

    def list_goals(self, user_id: int, role_key: str) -> list[WorkbenchGoalRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM workbench_goals WHERE user_id = ? AND role_key = ? "
                "ORDER BY sort_order ASC, id ASC",
                (user_id, role_key),
            ).fetchall()
        return map_rows(rows, WorkbenchGoalRow)

    def create_goal(
        self,
        *,
        user_id: int,
        role_key: str,
        title: str,
        target_value: float,
        current_value: float,
        unit: str = "",
        period: str = "month",
        sort_order: int = 0,
    ) -> WorkbenchGoalRow:
        ts = now_ts()
        with self._db.transaction() as conn:
            goal_id = insert_returning_id(
                conn,
                "INSERT INTO workbench_goals("
                "user_id, role_key, title, target_value, current_value, unit, period, "
                "sort_order, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    role_key,
                    title,
                    target_value,
                    current_value,
                    unit,
                    period,
                    sort_order,
                    ts,
                ),
            )
            row = conn.execute("SELECT * FROM workbench_goals WHERE id = ?", (goal_id,)).fetchone()
        assert row is not None
        return WorkbenchGoalRow.from_row(row)

    def update_goal(
        self,
        user_id: int,
        goal_id: int,
        *,
        current_value: float | None = None,
        target_value: float | None = None,
    ) -> WorkbenchGoalRow | None:
        fields: list[str] = []
        params: list[Any] = []
        if current_value is not None:
            fields.append("current_value = ?")
            params.append(current_value)
        if target_value is not None:
            fields.append("target_value = ?")
            params.append(target_value)
        if not fields:
            with self._db.connect() as conn:
                row = conn.execute(
                    "SELECT * FROM workbench_goals WHERE id = ? AND user_id = ?",
                    (goal_id, user_id),
                ).fetchone()
            return WorkbenchGoalRow.from_row(row) if row else None
        fields.append("updated_at = ?")
        params.append(now_ts())
        with self._db.transaction() as conn:
            conn.execute(
                f"UPDATE workbench_goals SET {', '.join(fields)} WHERE id = ? AND user_id = ?",
                (*params, goal_id, user_id),
            )
            row = conn.execute(
                "SELECT * FROM workbench_goals WHERE id = ? AND user_id = ?",
                (goal_id, user_id),
            ).fetchone()
        return WorkbenchGoalRow.from_row(row) if row else None
