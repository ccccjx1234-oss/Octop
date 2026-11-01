"""Turn usage_log token totals into cost with admin-entered model prices.

Prices are per million tokens. A usage row's ``model`` is usually
``provider/model`` (sometimes ``provider:model``); a price row matches that
exact ref first, then the bare model id after the separator, case-insensitively. Rows with no price count as
zero cost and are flagged ``priced: false`` so the page can say so.
"""

from __future__ import annotations

from typing import Any

from octop.infra.db.repos.model_pricing import ModelPriceRow
from octop.infra.errors import ErrorCode, OctopError

_PER = 1_000_000


def _lookup(prices: dict[str, ModelPriceRow], model: str) -> ModelPriceRow | None:
    key = model.strip().lower()
    if key in prices:
        return prices[key]
    for sep in ("/", ":"):
        _, _, bare = key.partition(sep)
        if bare and bare in prices:
            return prices[bare]
    return None


def row_cost(price: ModelPriceRow | None, totals: dict[str, Any]) -> float:
    if price is None:
        return 0.0
    return (
        int(totals.get("uncached_input_tokens", 0)) * price.input_per_mtok
        + int(totals.get("cache_read_tokens", 0)) * price.cache_read_per_mtok
        # No separate cache-write rate exists; bill writes as input tokens.
        + int(totals.get("cache_write_tokens", 0)) * price.input_per_mtok
        + int(totals.get("output_tokens", 0)) * price.output_per_mtok
    ) / _PER


def cost_report(repos: Any, *, window: str, timezone: str) -> dict[str, Any]:
    """Cost per model and per user over *window*."""
    prices = {row.model.lower(): row for row in repos.model_pricing_repo.list_all()}
    rows = repos.usage_repo.totals_by_user_model(window=window, timezone=timezone)
    usernames = {int(u.id): u.username for u in repos.user_repo.list(include_disabled=True)}
    currencies = {row.currency for row in prices.values()} or {"CNY"}

    by_model: dict[str, dict[str, Any]] = {}
    by_user: dict[int, dict[str, Any]] = {}
    total = 0.0
    for row in rows:
        price = _lookup(prices, row["model"])
        cost = row_cost(price, row)
        total += cost
        model = by_model.setdefault(
            row["model"],
            {"model": row["model"], "total_tokens": 0, "cost": 0.0, "priced": price is not None},
        )
        model["total_tokens"] += row["total_tokens"]
        model["cost"] += cost
        user = by_user.setdefault(
            row["user_id"],
            {
                "user_id": row["user_id"],
                "username": usernames.get(row["user_id"], str(row["user_id"])),
                "total_tokens": 0,
                "cost": 0.0,
            },
        )
        user["total_tokens"] += row["total_tokens"]
        user["cost"] += cost

    def _rounded(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for item in items:
            item["cost"] = round(item["cost"], 4)
        return sorted(items, key=lambda item: (-item["cost"], -item["total_tokens"]))

    return {
        "window": window,
        "currency": sorted(currencies)[0] if len(currencies) == 1 else "mixed",
        "total_cost": round(total, 4),
        "by_model": _rounded(list(by_model.values())),
        "by_user": _rounded(list(by_user.values())),
    }


def save_price(
    repos: Any,
    *,
    model: str,
    input_per_mtok: float,
    output_per_mtok: float,
    cache_read_per_mtok: float,
    currency: str,
) -> None:
    ref = model.strip()
    if not ref:
        raise OctopError(ErrorCode.INVALID_ARGUMENT, "model is required")
    if min(input_per_mtok, output_per_mtok, cache_read_per_mtok) < 0:
        raise OctopError(ErrorCode.INVALID_ARGUMENT, "price cannot be negative")
    repos.model_pricing_repo.upsert(
        model=ref,
        input_per_mtok=input_per_mtok,
        output_per_mtok=output_per_mtok,
        cache_read_per_mtok=cache_read_per_mtok,
        currency=(currency.strip().upper() or "CNY")[:8],
    )


__all__ = ["cost_report", "row_cost", "save_price"]
