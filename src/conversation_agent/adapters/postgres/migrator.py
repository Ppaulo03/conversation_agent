"""Schema migration tooling (Phase 10): forward-only, checksummed, serialized.

- FORWARD-ONLY: a migration is never edited after it ran and there are no "down" scripts; going
  back means restoring a backup or writing a new forward migration (expand/contract: add the new
  shape, deploy code that uses it, only later drop the old one).
- CHECKSUMS: each applied migration records the hash of its file. If a file changes afterwards
  the database and the code disagree about what the schema is: nothing is applied until a person
  looks (`MigrationDriftError`).
- SERIALIZED: one migrator at a time (an advisory lock), so two instances booting together do not
  race, and each migration runs in its own transaction.
- A DATABASE AHEAD of this build (it holds a migration this code does not know) is refused: old
  code must not run against a schema it does not understand.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from typing import Literal

import asyncpg

from conversation_agent.core.errors import ConversationAgentError

_PACKAGE = "conversation_agent.adapters.postgres.migrations"
_LOCK = "conversation_agent.migrate"

State = Literal["applied", "pending", "changed", "unknown"]


class MigrationDriftError(ConversationAgentError):
    """An applied migration no longer matches its file."""


class SchemaAheadError(ConversationAgentError):
    """The database holds migrations this build does not have."""


class SchemaOutOfDateError(ConversationAgentError):
    """Pending migrations: the schema is behind this build."""


@dataclass(frozen=True)
class MigrationStatus:
    version: str
    state: State  # applied | pending | changed (file differs) | unknown (no file: db is ahead)
    applied_at: datetime | None = None


def _files() -> dict[str, str]:
    found = {
        f.name.removesuffix(".sql"): f.read_text(encoding="utf-8")
        for f in resources.files(_PACKAGE).iterdir()
        if f.name.endswith(".sql")
    }
    return dict(sorted(found.items()))


def checksum(sql: str) -> str:
    return hashlib.sha256(sql.replace("\r\n", "\n").encode("utf-8")).hexdigest()


class Migrator:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def status(self) -> list[MigrationStatus]:
        async with self._pool.acquire() as conn:
            await self._bootstrap(conn)
            return await self._status(conn)

    async def apply(self, *, dry_run: bool = False) -> list[str]:
        """Applies pending migrations in order and returns their versions (with `dry_run`, the
        versions that WOULD be applied). Refuses drift and a database that is ahead."""
        async with self._pool.acquire() as conn:
            await self._bootstrap(conn)
            await conn.execute("SELECT pg_advisory_lock(hashtext($1))", _LOCK)
            try:
                return await self._apply(conn, dry_run)
            finally:
                await conn.execute("SELECT pg_advisory_unlock(hashtext($1))", _LOCK)

    async def ensure_current(self) -> None:
        """Startup guard: the schema is exactly what this build expects (no drift, none pending,
        not ahead). Raises instead of letting the application run on a schema it does not know."""
        problems = [s for s in await self.status() if s.state != "applied"]
        for state, error in (
            ("changed", MigrationDriftError),
            ("unknown", SchemaAheadError),
            ("pending", SchemaOutOfDateError),
        ):
            names = [s.version for s in problems if s.state == state]
            if names:
                raise error(f"{state} migrations: {', '.join(names)}")

    # ------------------------------------------------------------------ internals

    @staticmethod
    async def _bootstrap(conn: asyncpg.Connection) -> None:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        await conn.execute("ALTER TABLE schema_migrations ADD COLUMN IF NOT EXISTS checksum text")

    @staticmethod
    async def _status(conn: asyncpg.Connection) -> list[MigrationStatus]:
        files = _files()
        rows = {
            r["version"]: r
            for r in await conn.fetch("SELECT version, applied_at, checksum FROM schema_migrations")
        }
        out: list[MigrationStatus] = []
        for version, sql in files.items():
            row = rows.get(version)
            if row is None:
                out.append(MigrationStatus(version, "pending"))
            elif row["checksum"] is not None and row["checksum"] != checksum(sql):
                out.append(MigrationStatus(version, "changed", row["applied_at"]))
            else:  # a row from before checksums existed is trusted once and stamped by `apply`
                out.append(MigrationStatus(version, "applied", row["applied_at"]))
        out += [
            MigrationStatus(v, "unknown", r["applied_at"])
            for v, r in sorted(rows.items())
            if v not in files
        ]
        return out

    async def _apply(self, conn: asyncpg.Connection, dry_run: bool) -> list[str]:
        statuses = await self._status(conn)
        drifted = [s.version for s in statuses if s.state == "changed"]
        if drifted:
            raise MigrationDriftError(f"applied migrations were edited: {', '.join(drifted)}")
        ahead = [s.version for s in statuses if s.state == "unknown"]
        if ahead:
            raise SchemaAheadError(f"the database holds migrations this build lacks: {ahead}")
        files = _files()
        pending = [s.version for s in statuses if s.state == "pending"]
        if dry_run:
            return pending
        for status in statuses:  # trust-on-first-use for rows that predate checksums
            if status.state == "applied":
                await conn.execute(
                    "UPDATE schema_migrations SET checksum = $2 WHERE version = $1 "
                    "AND checksum IS NULL",
                    status.version,
                    checksum(files[status.version]),
                )
        for version in pending:
            async with conn.transaction():
                await conn.execute(files[version])
                await conn.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES ($1, $2)",
                    version,
                    checksum(files[version]),
                )
        return pending
