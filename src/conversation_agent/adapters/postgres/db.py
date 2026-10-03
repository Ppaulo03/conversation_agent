"""Connection pool + forward-only SQL migrations."""

from __future__ import annotations

import json
from importlib import resources

import asyncpg

_MIGRATIONS_PACKAGE = "conversation_agent.adapters.postgres.migrations"


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
        """Applies pending migrations in filename order; returns the versions applied."""
        applied: list[str] = []
        files = sorted(
            (f for f in resources.files(_MIGRATIONS_PACKAGE).iterdir() if f.name.endswith(".sql")),
            key=lambda f: f.name,
        )
        async with self.pool.acquire() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
            for f in files:
                version = f.name.removesuffix(".sql")
                if version in done:
                    continue
                async with conn.transaction():
                    await conn.execute(f.read_text(encoding="utf-8"))
                    await conn.execute(
                        "INSERT INTO schema_migrations (version) VALUES ($1)", version
                    )
                applied.append(version)
        return applied
