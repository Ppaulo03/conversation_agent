"""PostgreSQL test fixtures: a dedicated `*_test` database, migrated once per session and
truncated before every test. Skips (loudly) when Postgres is not reachable."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

import asyncpg
import pytest

from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.postgres.conversations import ensure_conversation
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.models.conversation import ConversationIdentity
from support.builders import IDENTITY

TEST_DB_NAME = "conversation_agent_test"
T0 = datetime(2026, 10, 5, 8, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))


def _dsn(database: str) -> str:
    user = os.environ.get("POSTGRES_USER", "conversation_agent")
    password = os.environ.get("POSTGRES_PASSWORD", "conversation_agent_dev")
    port = os.environ.get("POSTGRES_PORT", "5432")
    return f"postgresql://{user}:{password}@127.0.0.1:{port}/{database}"


async def _prepare_database() -> str:
    admin = await asyncpg.connect(_dsn("postgres"), timeout=5)
    try:
        exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", TEST_DB_NAME)
        if not exists:
            await admin.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    finally:
        await admin.close()
    dsn = os.environ.get("TEST_DATABASE_URL") or _dsn(TEST_DB_NAME)
    conn = await asyncpg.connect(dsn, timeout=5)
    try:  # always start from the latest migrations
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    finally:
        await conn.close()
    db = await PostgresDatabase.connect(dsn, max_size=2)
    await db.migrate()
    await db.close()
    return dsn


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    try:
        yield asyncio.run(_prepare_database())
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        pytest.skip(f"PostgreSQL not available ({type(exc).__name__}); run `docker compose up -d`")


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[PostgresDatabase]:
    database = await PostgresDatabase.connect(pg_dsn, max_size=10)
    await database.pool.execute(
        "TRUNCATE conversation_states, turns, turn_journal, inbox_events, outbox_messages, "
        "tool_invocations, scheduled_events, pending_actions, action_confirmations "
        "RESTART IDENTITY"
    )
    yield database
    await database.close()


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)


@pytest.fixture
def identity() -> ConversationIdentity:
    return IDENTITY


@pytest.fixture
async def conversation(db: PostgresDatabase, clock: FixedClock, identity: ConversationIdentity):  # type: ignore[no-untyped-def]
    await ensure_conversation(db.pool, identity, clock.now())
    return identity
