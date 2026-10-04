from __future__ import annotations

from datetime import timedelta

from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.ports.ratelimit import RateDecision, RateLimit


class PostgresRateLimiter:
    """Token buckets shared by every worker. One transaction per decision, serialized per bucket
    by an advisory lock; the clock is the database's."""

    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db

    async def acquire(
        self, scope: str, key: str, limit: RateLimit, *, cost: float = 1.0
    ) -> RateDecision:
        if cost > limit.capacity:
            return RateDecision(False, float("inf"))
        async with self._db.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"rate/{scope}/{key}")
            row = await conn.fetchrow(
                "SELECT tokens, "
                "EXTRACT(EPOCH FROM clock_timestamp() - refilled_at)::float8 AS idle "
                "FROM rate_limit_buckets WHERE scope = $1 AND key = $2",
                scope,
                key,
            )
            tokens = (
                limit.capacity
                if row is None
                else min(limit.capacity, row["tokens"] + row["idle"] * limit.refill_per_second)
            )
            allowed = tokens >= cost
            await conn.execute(
                "INSERT INTO rate_limit_buckets (scope, key, tokens, refilled_at) "
                "VALUES ($1, $2, $3, clock_timestamp()) ON CONFLICT (scope, key) DO UPDATE "
                "SET tokens = $3, refilled_at = clock_timestamp()",
                scope,
                key,
                tokens - cost if allowed else tokens,
            )
            if allowed:
                return RateDecision(True)
            return RateDecision(False, (cost - tokens) / limit.refill_per_second)

    async def purge(self, older_than: timedelta) -> int:
        """Forget buckets nobody touched for a while (a full bucket is the same as no bucket)."""
        status = await self._db.pool.execute(
            "DELETE FROM rate_limit_buckets WHERE refilled_at < clock_timestamp() - $1::interval",
            older_than,
        )
        return int(status.split()[-1])
