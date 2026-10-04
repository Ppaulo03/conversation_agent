"""`python -m conversation_agent.app.integrity`: check the durable state (database from
`DATABASE_URL`). Exit codes: 0 healthy, 1 violations or a wrong schema, 2 cannot run.
Run it after a restore, after an incident, and before a release.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Sequence

from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.integrity import verify_integrity


async def run(dsn: str) -> int:
    db = await PostgresDatabase.connect(dsn, max_size=2)
    try:
        report = await verify_integrity(db)
    finally:
        await db.close()
    if report.schema_problem:
        print(f"schema: {report.schema_problem}", file=sys.stderr)
        return 1
    for violation in report.violations:
        print(f"{violation.code}: {violation.count} ({violation.meaning})", file=sys.stderr)
        for sample in violation.sample:
            print(f"    {sample}", file=sys.stderr)
    if report.ok:
        print(f"ok: {len(report.checked)} checks")
        return 0
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    if list(sys.argv[1:] if argv is None else argv):
        print("usage: python -m conversation_agent.app.integrity", file=sys.stderr)
        return 2
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(dsn))
    except OSError as exc:
        print(f"cannot reach the database: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
