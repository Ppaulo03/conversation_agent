"""LLM prices as data, applied when reading the books (never baked into the records).

A record stores tokens; cost is tokens x price AT THE TIME of the call. Keeping the two apart means
a corrected or new price re-prices history without rewriting it, and a model nobody priced is
reported as UNPRICED (a visible gap), not as free.

Prices are per million tokens, in one currency per table. They are an operator-maintained fact
about a provider's price list, not something this code knows: `ops/llm_prices.yaml` is where they
live and where they are kept up to date.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from conversation_agent.core.models.llm_usage import LLMCallRecord, UsageRow


class ModelPrice(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    effective_from: date
    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)
    # Unset: a cache read costs the input price, a cache write too (no discount assumed).
    cache_read_per_mtok: float | None = Field(default=None, ge=0)
    cache_write_per_mtok: float | None = Field(default=None, ge=0)


class PriceTable(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    currency: str = "USD"
    # key: "<provider>/<model>" or just "<model>" (the provider-qualified key wins)
    models: dict[str, tuple[ModelPrice, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _ordered(self) -> PriceTable:
        for name, prices in self.models.items():
            dates = [p.effective_from for p in prices]
            if len(set(dates)) != len(dates):
                raise ValueError(f"{name}: two prices with the same effective_from")
        return self

    def price_for(self, provider: str | None, model: str | None, at: datetime) -> ModelPrice | None:
        if model is None:
            return None
        for key in ((f"{provider}/{model}" if provider else None), model):
            prices = self.models.get(key) if key else None
            if prices:
                valid = [p for p in prices if p.effective_from <= at.date()]
                if valid:
                    return max(valid, key=lambda p: p.effective_from)
        return None


def cost_of(
    prices: PriceTable,
    *,
    provider: str | None,
    model: str | None,
    at: datetime,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float | None:
    """Estimated cost, or None when the model has no price on that date."""
    price = prices.price_for(provider, model, at)
    if price is None:
        return None
    read = price.input_per_mtok if price.cache_read_per_mtok is None else price.cache_read_per_mtok
    write = (
        price.input_per_mtok if price.cache_write_per_mtok is None else price.cache_write_per_mtok
    )
    return (
        input_tokens * price.input_per_mtok
        + output_tokens * price.output_per_mtok
        + cache_read_tokens * read
        + cache_write_tokens * write
    ) / 1_000_000


def cost_of_record(prices: PriceTable, record: LLMCallRecord) -> float | None:
    return cost_of(
        prices,
        provider=record.provider,
        model=record.model,
        at=record.started_at,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        cache_read_tokens=record.cache_read_tokens,
        cache_write_tokens=record.cache_write_tokens,
    )


def rollup(rows: list[UsageRow], group_by: tuple[str, ...]) -> list[UsageRow]:
    """Merges rows that differ only in provider/model (needed for pricing) when the caller did
    not ask to group by them. Percentiles cannot be merged exactly: the merged row keeps the worst
    (highest) of the parts, which is the conservative reading for a latency SLO."""
    keep_model = "model" in group_by
    keep_provider = "provider" in group_by or keep_model
    merged: dict[tuple[tuple[str, str | None], ...], UsageRow] = {}
    for row in rows:
        key = tuple(sorted(row.keys.items())) + ((("model", row.model),) if keep_model else ())
        key += (("provider", row.provider),) if keep_provider else ()
        into = merged.get(key)
        if into is None:
            merged[key] = row.model_copy(
                update={
                    "provider": row.provider if keep_provider else None,
                    "model": row.model if keep_model else None,
                }
            )
            continue
        merged[key] = into.model_copy(
            update={
                "calls": into.calls + row.calls,
                "errors": into.errors + row.errors,
                "input_tokens": into.input_tokens + row.input_tokens,
                "output_tokens": into.output_tokens + row.output_tokens,
                "cache_read_tokens": into.cache_read_tokens + row.cache_read_tokens,
                "cache_write_tokens": into.cache_write_tokens + row.cache_write_tokens,
                "reasoning_tokens": into.reasoning_tokens + row.reasoning_tokens,
                "latency_p50_ms": max(into.latency_p50_ms, row.latency_p50_ms),
                "latency_p95_ms": max(into.latency_p95_ms, row.latency_p95_ms),
                "cost_usd": into.cost_usd + row.cost_usd,
                "unpriced_calls": into.unpriced_calls + row.unpriced_calls,
            }
        )
    return list(merged.values())
