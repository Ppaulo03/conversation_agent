from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.llm_compare import VersionCost
from conversation_agent.core.llm_prices import PriceTable, cost_of, rollup
from conversation_agent.core.models.llm_usage import LLMCallRecord, UsageQuery, UsageRow

# group key -> SQL expression
_KEYS: dict[str, str] = {
    "day": "to_char(date_trunc('day', started_at AT TIME ZONE 'UTC'), 'YYYY-MM-DD')",
    "agent_id": "agent_id",
    "agent_version": "agent_version",
    "provider": "provider",
    "model": "model",
    "purpose": "purpose",
}


class PostgresLLMUsageStore:
    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db

    async def record(self, call: LLMCallRecord) -> None:
        await self._db.pool.execute(
            "INSERT INTO llm_usage (tenant_id, started_at, purpose, agent_id, agent_version, "
            "conversation_ref, turn_id, trace_id, request_id, provider, model, input_tokens, "
            "output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, latency_ms, "
            "outcome, error_code, stop_reason, audio_seconds) VALUES "
            "($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21)",
            call.tenant_id,
            call.started_at,
            call.purpose,
            call.agent_id,
            call.agent_version,
            call.conversation_ref,
            call.turn_id,
            call.trace_id,
            call.request_id,
            call.provider,
            call.model,
            call.input_tokens,
            call.output_tokens,
            call.cache_read_tokens,
            call.cache_write_tokens,
            call.reasoning_tokens,
            call.latency_ms,
            call.outcome,
            call.error_code,
            call.stop_reason,
            call.audio_seconds,
        )

    async def report(self, query: UsageQuery, prices: PriceTable) -> list[UsageRow]:
        # Internally ALWAYS by (day, provider, model) so each group is priced at the price in
        # force on its day; the requested grouping is applied afterwards by `rollup`.
        wanted = [k for k in query.group_by if k not in ("day", "provider", "model")]
        select_keys: list[str] = ["day", *wanted]
        columns = ", ".join(f"{_KEYS[k]} AS k_{k}" for k in select_keys)
        group = ", ".join(f"k_{k}" for k in select_keys)
        rows = await self._db.pool.fetch(
            f"SELECT {columns}, provider, model, count(*) AS calls, "
            "count(*) FILTER (WHERE outcome = 'error') AS errors, "
            "sum(input_tokens)::bigint AS input_tokens, "
            "sum(output_tokens)::bigint AS output_tokens, "
            "sum(cache_read_tokens)::bigint AS cache_read_tokens, "
            "sum(cache_write_tokens)::bigint AS cache_write_tokens, "
            "sum(reasoning_tokens)::bigint AS reasoning_tokens, "
            "sum(audio_seconds)::float8 AS audio_seconds, "
            "(percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms))::float8 AS p50, "
            "(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms))::float8 AS p95 "
            "FROM llm_usage WHERE tenant_id = $1 AND started_at >= $2 AND started_at < $3 "
            "AND ($4::text IS NULL OR agent_id = $4) "
            f"GROUP BY {group}, provider, model",
            query.tenant_id,
            query.since,
            query.until,
            query.agent_id,
        )
        out = [self._row(row, select_keys, prices) for row in rows]
        if "day" not in query.group_by:  # the day was only for pricing
            out = [
                r.model_copy(update={"keys": {k: v for k, v in r.keys.items() if k != "day"}})
                for r in out
            ]
        return rollup(out, query.group_by)

    @staticmethod
    def _row(row: Any, keys: list[str], prices: PriceTable) -> UsageRow:
        day = datetime.fromisoformat(row["k_day"]).replace(tzinfo=UTC)
        cost = cost_of(
            prices,
            provider=row["provider"],
            model=row["model"],
            at=day,
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            cache_read_tokens=int(row["cache_read_tokens"]),
            cache_write_tokens=int(row["cache_write_tokens"]),
            audio_seconds=float(row["audio_seconds"] or 0),
        )
        return UsageRow(
            keys={k: row[f"k_{k}"] for k in keys},
            provider=row["provider"],
            model=row["model"],
            calls=int(row["calls"]),
            errors=int(row["errors"]),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            cache_read_tokens=int(row["cache_read_tokens"]),
            cache_write_tokens=int(row["cache_write_tokens"]),
            reasoning_tokens=int(row["reasoning_tokens"]),
            audio_seconds=float(row["audio_seconds"] or 0),
            latency_p50_ms=float(row["p50"] or 0),
            latency_p95_ms=float(row["p95"] or 0),
            cost_usd=cost or 0.0,
            unpriced_calls=int(row["calls"]) if cost is None else 0,
        )

    async def compare_versions(
        self,
        tenant_id: str,
        agent_id: str,
        versions: list[str],
        since: datetime,
        until: datetime,
        prices: PriceTable,
    ) -> list[VersionCost]:
        """Cost and calls PER TURN for the given versions of one agent (what a canary compares)."""
        rows = await self.report(
            UsageQuery(
                tenant_id=tenant_id,
                since=since,
                until=until,
                group_by=("agent_version",),
                agent_id=agent_id,
            ),
            prices,
        )
        distinct = await self._db.pool.fetch(
            "SELECT agent_version, count(DISTINCT turn_id) AS turns, "
            "count(DISTINCT conversation_ref) AS conversations FROM llm_usage "
            "WHERE tenant_id = $1 AND agent_id = $2 AND started_at >= $3 AND started_at < $4 "
            "AND agent_version = ANY($5::text[]) GROUP BY agent_version",
            tenant_id,
            agent_id,
            since,
            until,
            versions,
        )
        counts = {r["agent_version"]: r for r in distinct}
        out: list[VersionCost] = []
        for version in versions:
            row = next((r for r in rows if r.keys.get("agent_version") == version), None)
            seen = counts.get(version)
            out.append(
                VersionCost(
                    agent_version=version,
                    turns=int(seen["turns"]) if seen else 0,
                    conversations=int(seen["conversations"]) if seen else 0,
                    calls=row.calls if row else 0,
                    errors=row.errors if row else 0,
                    tokens=(
                        row.input_tokens
                        + row.output_tokens
                        + row.cache_read_tokens
                        + row.cache_write_tokens
                        if row
                        else 0
                    ),
                    cost_usd=row.cost_usd if row else 0.0,
                    unpriced_calls=row.unpriced_calls if row else 0,
                    latency_p95_ms=row.latency_p95_ms if row else 0.0,
                )
            )
        return out
