"""`python -m conversation_agent.app.migrate status|check|apply [--dry-run]`.

The database comes from `DATABASE_URL`. Exit codes: 0 ok, 1 the schema needs attention (pending,
drift or ahead), 2 usage or connection error. Meant for deploy pipelines: `check` before starting a
release, `apply` as an explicit step, never as a side effect of booting many instances.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Sequence

from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.migrator import Migrator
from conversation_agent.core.errors import ConversationAgentError

USAGE = "usage: python -m conversation_agent.app.migrate status|check|apply [--dry-run]"


async def run(command: str, dsn: str, dry_run: bool) -> int:
    db = await PostgresDatabase.connect(dsn, max_size=2)
    try:
        migrator = Migrator(db.pool)
        if command == "status":
            statuses = await migrator.status()
            for s in statuses:
                print(f"{s.version:<40} {s.state}")
            return 0 if all(s.state == "applied" for s in statuses) else 1
        if command == "check":
            await migrator.ensure_current()
            print("schema is current")
            return 0
        applied = await migrator.apply(dry_run=dry_run)
        verb = "would apply" if dry_run else "applied"
        print(f"{verb}: {', '.join(applied) if applied else 'nothing'}")
        return 0
    except ConversationAgentError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await db.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]
    if len(args) != 1 or args[0] not in ("status", "check", "apply"):
        print(USAGE, file=sys.stderr)
        return 2
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(args[0], dsn, dry_run))
    except OSError as exc:
        print(f"cannot reach the database: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
