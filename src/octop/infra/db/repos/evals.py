"""Eval cases, eval runs, and per-case results."""

from __future__ import annotations

import json
from dataclasses import dataclass

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, now_ts
from octop.infra.utils.ulid import new_ulid


def _str_list(raw: object) -> list[str]:
    try:
        value = json.loads(str(raw or "[]"))
    except ValueError:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


@dataclass(frozen=True)
class EvalCaseRow:
    eval_case_id: str
    suite: str
    prompt: str
    expect_contains: list[str]
    expect_not_contains: list[str]
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> EvalCaseRow:
        return cls(
            eval_case_id=str(row["eval_case_id"]),
            suite=str(row["suite"]),
            prompt=str(row["prompt"]),
            expect_contains=_str_list(row["expect_contains"]),
            expect_not_contains=_str_list(row["expect_not_contains"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )


@dataclass(frozen=True)
class EvalRunRow:
    eval_run_id: str
    agent_id: str
    suite: str
    status: str
    total: int
    passed: int
    error: str
    started_at: int
    finished_at: int | None

    @classmethod
    def from_row(cls, row: DbRow) -> EvalRunRow:
        finished = row["finished_at"]
        return cls(
            eval_run_id=str(row["eval_run_id"]),
            agent_id=str(row["agent_id"]),
            suite=str(row["suite"]),
            status=str(row["status"]),
            total=int(row["total"] or 0),
            passed=int(row["passed"] or 0),
            error=str(row["error"] or ""),
            started_at=int(row["started_at"]),
            finished_at=int(finished) if finished is not None else None,
        )


@dataclass(frozen=True)
class EvalResultRow:
    eval_case_id: str
    prompt: str
    passed: bool
    output: str
    detail: str
    latency_ms: int

    @classmethod
    def from_row(cls, row: DbRow) -> EvalResultRow:
        return cls(
            eval_case_id=str(row["eval_case_id"]),
            prompt=str(row["prompt"]),
            passed=bool(int(row["passed"])),
            output=str(row["output"] or ""),
            detail=str(row["detail"] or ""),
            latency_ms=int(row["latency_ms"] or 0),
        )


class EvalRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    # --- cases ------------------------------------------------------------

    def list_cases(self, suite: str | None = None) -> list[EvalCaseRow]:
        with self._db.connect() as conn:
            if suite is None:
                rows = conn.execute("SELECT * FROM eval_cases ORDER BY suite, id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM eval_cases WHERE suite = ? ORDER BY id", (suite,)
                ).fetchall()
        return map_rows(rows, EvalCaseRow)

    def get_case(self, eval_case_id: str) -> EvalCaseRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM eval_cases WHERE eval_case_id = ?", (eval_case_id,)
            ).fetchone()
        return EvalCaseRow.from_row(row) if row else None

    def create_case(
        self,
        *,
        suite: str,
        prompt: str,
        expect_contains: list[str],
        expect_not_contains: list[str],
        created_by: int | None,
    ) -> EvalCaseRow:
        ts = now_ts()
        case_id = new_ulid()
        with self._db.connect() as conn:
            conn.execute(
                """
                INSERT INTO eval_cases (
                    eval_case_id, suite, prompt, expect_contains, expect_not_contains,
                    created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case_id,
                    suite,
                    prompt,
                    json.dumps(expect_contains, ensure_ascii=False),
                    json.dumps(expect_not_contains, ensure_ascii=False),
                    created_by,
                    ts,
                    ts,
                ),
            )
        row = self.get_case(case_id)
        assert row is not None
        return row

    def update_case(
        self,
        eval_case_id: str,
        *,
        suite: str,
        prompt: str,
        expect_contains: list[str],
        expect_not_contains: list[str],
    ) -> EvalCaseRow | None:
        with self._db.connect() as conn:
            conn.execute(
                """
                UPDATE eval_cases SET suite = ?, prompt = ?, expect_contains = ?,
                    expect_not_contains = ?, updated_at = ?
                WHERE eval_case_id = ?
                """,
                (
                    suite,
                    prompt,
                    json.dumps(expect_contains, ensure_ascii=False),
                    json.dumps(expect_not_contains, ensure_ascii=False),
                    now_ts(),
                    eval_case_id,
                ),
            )
        return self.get_case(eval_case_id)

    def delete_case(self, eval_case_id: str) -> None:
        with self._db.connect() as conn:
            conn.execute("DELETE FROM eval_cases WHERE eval_case_id = ?", (eval_case_id,))

    # --- runs -------------------------------------------------------------

    def create_run(self, *, agent_id: str, suite: str, total: int, started_by: int) -> EvalRunRow:
        run_id = new_ulid()
        with self._db.connect() as conn:
            conn.execute(
                """
                INSERT INTO eval_runs (eval_run_id, agent_id, suite, status, total, started_by,
                    started_at)
                VALUES (?, ?, ?, 'running', ?, ?, ?)
                """,
                (run_id, agent_id, suite, total, started_by, now_ts()),
            )
        row = self.get_run(run_id)
        assert row is not None
        return row

    def get_run(self, eval_run_id: str) -> EvalRunRow | None:
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM eval_runs WHERE eval_run_id = ?", (eval_run_id,)
            ).fetchone()
        return EvalRunRow.from_row(row) if row else None

    def list_runs(self, limit: int = 50) -> list[EvalRunRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM eval_runs ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return map_rows(rows, EvalRunRow)

    def add_result(
        self,
        eval_run_id: str,
        *,
        eval_case_id: str,
        prompt: str,
        passed: bool,
        output: str,
        detail: str,
        latency_ms: int,
    ) -> None:
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO eval_results (eval_run_id, eval_case_id, prompt, passed, output,
                    detail, latency_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (eval_run_id, eval_case_id, prompt, int(passed), output, detail, latency_ms),
            )
            if passed:
                conn.execute(
                    "UPDATE eval_runs SET passed = passed + 1 WHERE eval_run_id = ?",
                    (eval_run_id,),
                )

    def finish_run(self, eval_run_id: str, *, status: str, error: str = "") -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE eval_runs SET status = ?, error = ?, finished_at = ? WHERE eval_run_id = ?",
                (status, error, now_ts(), eval_run_id),
            )

    def fail_stale_runs(self) -> None:
        """Mark runs left ``running`` by a previous process as failed."""
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE eval_runs SET status = 'failed', error = 'interrupted by restart',"
                " finished_at = ? WHERE status = 'running'",
                (now_ts(),),
            )

    def list_results(self, eval_run_id: str) -> list[EvalResultRow]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM eval_results WHERE eval_run_id = ? ORDER BY eval_case_id",
                (eval_run_id,),
            ).fetchall()
        return map_rows(rows, EvalResultRow)
