"""Per-model token prices used to turn usage rows into cost."""

from __future__ import annotations

from dataclasses import dataclass

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, now_ts


@dataclass(frozen=True)
class ModelPriceRow:
    model: str
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    currency: str
    updated_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> ModelPriceRow:
        return cls(
            model=str(row["model"]),
            input_per_mtok=float(row["input_per_mtok"] or 0),
            output_per_mtok=float(row["output_per_mtok"] or 0),
            cache_read_per_mtok=float(row["cache_read_per_mtok"] or 0),
            currency=str(row["currency"] or "CNY"),
            updated_at=int(row["updated_at"]),
        )


class ModelPricingRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def list_all(self) -> list[ModelPriceRow]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM model_pricing ORDER BY model").fetchall()
        return map_rows(rows, ModelPriceRow)

    def upsert(
        self,
        *,
        model: str,
        input_per_mtok: float,
        output_per_mtok: float,
        cache_read_per_mtok: float,
        currency: str,
    ) -> None:
        with self._db.connect() as conn:
            conn.execute(
                """
                INSERT INTO model_pricing (
                    model, input_per_mtok, output_per_mtok, cache_read_per_mtok,
                    currency, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (model) DO UPDATE SET
                    input_per_mtok = excluded.input_per_mtok,
                    output_per_mtok = excluded.output_per_mtok,
                    cache_read_per_mtok = excluded.cache_read_per_mtok,
                    currency = excluded.currency,
                    updated_at = excluded.updated_at
                """,
                (
                    model,
                    float(input_per_mtok),
                    float(output_per_mtok),
                    float(cache_read_per_mtok),
                    currency,
                    now_ts(),
                ),
            )

    def delete(self, model: str) -> None:
        with self._db.connect() as conn:
            conn.execute("DELETE FROM model_pricing WHERE model = ?", (model,))
