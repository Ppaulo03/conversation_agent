"""Connection pool + forward-only SQL migrations (see `migrator`)."""

from __future__ import annotations

import json

import asyncpg

from conversation_agent.adapters.postgres.migrator import Migrator


async def _init_connection(conn: asyncpg.Connection) -> None:
    # jsonb <-> python objects (json text, not asyncpg's default str)
    for type_name in ("jsonb", "json"):
        await conn.set_type_codec(
            type_name,
            encoder=lambda v: json.dumps(v, ensure_ascii=False),
            decoder=json.loads,
            schema="pg_catalog",
        )


class PostgresDatabase:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str, *, min_size: int = 1, max_size: int = 10) -> PostgresDatabase:
        pool = await asyncpg.create_pool(
            dsn, min_size=min_size, max_size=max_size, init=_init_connection
        )
        assert pool is not None
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    async def migrate(self) -> list[str]:
        """Applies pending migrations (checksummed, serialized); returns the versions applied."""
        return await Migrator(self.pool).apply()
