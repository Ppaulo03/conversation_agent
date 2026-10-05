from __future__ import annotations

import math
from collections import defaultdict

from conversation_agent.core.llm_prices import PriceTable, cost_of, rollup
from conversation_agent.core.models.llm_usage import LLMCallRecord, UsageQuery, UsageRow


def _percentile(values: list[float], q: float) -> float:
    """Continuous percentile (the same definition PostgreSQL's percentile_cont uses)."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


class InMemoryLLMUsageStore:
    """The same contract as the durable ledger, for tests and single-process tooling."""

    def __init__(self) -> None:
        self.records: list[LLMCallRecord] = []

    async def record(self, call: LLMCallRecord) -> None:
        self.records.append(call)

    async def report(self, query: UsageQuery, prices: PriceTable) -> list[UsageRow]:
        wanted = [k for k in query.group_by if k not in ("day", "provider", "model")]
        groups: dict[tuple[str | None, ...], list[LLMCallRecord]] = defaultdict(list)
        for r in self.records:
            if r.tenant_id != query.tenant_id or not query.since <= r.started_at < query.until:
                continue
            if query.agent_id is not None and r.agent_id != query.agent_id:
                continue
            key = (
                r.started_at.strftime("%Y-%m-%d"),
                *(getattr(r, k) for k in wanted),
                r.provider,
                r.model,
            )
            groups[key].append(r)
        rows: list[UsageRow] = []
        for key, calls in groups.items():
            day, *rest = key
            *wanted_values, provider, model = rest
            keys: dict[str, str | None] = {"day": day}
            keys.update(dict(zip(wanted, wanted_values, strict=True)))
            totals = {
                f: sum(getattr(c, f) for c in calls)
                for f in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "reasoning_tokens",
                    "audio_seconds",
                )
            }
            at = calls[0].started_at.replace(hour=0, minute=0, second=0, microsecond=0)
            cost = cost_of(
                prices,
                provider=provider,
                model=model,
                at=at,
                input_tokens=totals["input_tokens"],
                output_tokens=totals["output_tokens"],
                cache_read_tokens=totals["cache_read_tokens"],
                cache_write_tokens=totals["cache_write_tokens"],
                audio_seconds=totals["audio_seconds"],
            )
            latencies = [c.latency_ms for c in calls]
            rows.append(
                UsageRow(
                    keys=keys,
                    provider=provider,
                    model=model,
                    calls=len(calls),
                    errors=sum(1 for c in calls if c.outcome == "error"),
                    latency_p50_ms=_percentile(latencies, 0.5),
                    latency_p95_ms=_percentile(latencies, 0.95),
                    cost_usd=cost or 0.0,
                    unpriced_calls=len(calls) if cost is None else 0,
                    **totals,
                )
            )
        if "day" not in query.group_by:
            rows = [
                r.model_copy(update={"keys": {k: v for k, v in r.keys.items() if k != "day"}})
                for r in rows
            ]
        return rollup(rows, query.group_by)
