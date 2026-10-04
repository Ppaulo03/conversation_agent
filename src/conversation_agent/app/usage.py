"""`python -m conversation_agent.app.usage --tenant T --since DATE --until DATE [--by KEYS]
[--agent ID] [--prices FILE] [--json]`: what the LLM calls consumed and cost.

With `--compare STABLE,CANDIDATE --agent ID` it compares two versions of an agent per TURN (cost,
model calls, errors) and exits 1 when the candidate looks worse: a gate for widening a canary.

KEYS is a comma list of: day, agent_id, agent_version, provider, model, purpose. Cost is tokens x
the price in force on each call's day, from the price file (default `ops/llm_prices.yaml`); a model
with no price is shown as UNPRICED and its cost is NOT included, so the totals are a lower bound
whenever that column is non-zero. The database comes from `DATABASE_URL`.
Exit codes: 0 ok, 2 usage / cannot run.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

from pydantic import ValidationError

from conversation_agent.adapters.observability.prices import load_prices
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.llm_usage import PostgresLLMUsageStore
from conversation_agent.core.errors import DefinitionError
from conversation_agent.core.llm_compare import cost_regressions
from conversation_agent.core.llm_prices import PriceTable
from conversation_agent.core.models.llm_usage import GroupBy, UsageQuery, UsageRow

USAGE = (
    "usage: python -m conversation_agent.app.usage --tenant T --since YYYY-MM-DD "
    "--until YYYY-MM-DD [--by KEYS] [--agent ID] [--prices FILE] [--json] "
    "[--compare STABLE,CANDIDATE]"
)


def _take(args: list[str], name: str) -> str | None:
    if name not in args:
        return None
    at = args.index(name)
    if at + 1 >= len(args):
        raise ValueError(f"{name} needs a value")
    value = args[at + 1]
    del args[at : at + 2]
    return value


def _day(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def format_table(rows: list[UsageRow], keys: tuple[str, ...]) -> str:
    header = [
        *keys,
        "calls",
        "errors",
        "in",
        "out",
        "cache_r",
        "cache_w",
        "p95_ms",
        "cost_usd",
        "unpriced",
    ]
    lines = [header]
    for r in sorted(rows, key=lambda r: tuple(str(r.keys.get(k)) for k in keys)):
        lines.append(
            [
                *(
                    str(r.keys.get(k) if k not in ("model", "provider") else getattr(r, k))
                    for k in keys
                ),
                str(r.calls),
                str(r.errors),
                str(r.input_tokens),
                str(r.output_tokens),
                str(r.cache_read_tokens),
                str(r.cache_write_tokens),
                f"{r.latency_p95_ms:.0f}",
                f"{r.cost_usd:.4f}",
                str(r.unpriced_calls),
            ]
        )
    widths = [max(len(line[i]) for line in lines) for i in range(len(header))]
    return "\n".join(
        "  ".join(c.ljust(w) for c, w in zip(line, widths, strict=True)) for line in lines
    )


async def compare(dsn: str, query: UsageQuery, versions: list[str], prices_path: str) -> int:
    prices = load_prices(prices_path) if Path(prices_path).exists() else PriceTable()
    db = await PostgresDatabase.connect(dsn, max_size=2)
    try:
        costs = await PostgresLLMUsageStore(db).compare_versions(
            query.tenant_id, query.agent_id or "", versions, query.since, query.until, prices
        )
    finally:
        await db.close()
    for c in costs:
        print(
            f"{c.agent_version:<12} turns={c.turns} calls/turn={c.calls_per_turn:.2f} "
            f"cost/turn={c.cost_per_turn:.5f} errors={c.error_rate:.1%} "
            f"p95={c.latency_p95_ms:.0f}ms"
        )
    problems = cost_regressions(costs[0], costs[1])
    for problem in problems:
        print(f"  ! {problem}")
    print("candidate looks worse" if problems else "no regression found")
    return 1 if problems else 0


async def run(dsn: str, query: UsageQuery, prices_path: str, as_json: bool) -> int:
    prices = load_prices(prices_path) if Path(prices_path).exists() else PriceTable()
    db = await PostgresDatabase.connect(dsn, max_size=2)
    try:
        rows = await PostgresLLMUsageStore(db).report(query, prices)
    finally:
        await db.close()
    if as_json:
        print(json.dumps([r.model_dump(mode="json") for r in rows], indent=2))
    else:
        print(format_table(rows, query.group_by) if rows else "no usage in this window")
        unpriced = sum(r.unpriced_calls for r in rows)
        if unpriced:
            print(f"\n{unpriced} call(s) have no price: the cost above is a lower bound.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]
    try:
        tenant = _take(args, "--tenant")
        since, until = _take(args, "--since"), _take(args, "--until")
        by = _take(args, "--by") or ""
        agent = _take(args, "--agent")
        prices = _take(args, "--prices") or "ops/llm_prices.yaml"
        versions = [v for v in (_take(args, "--compare") or "").split(",") if v]
        if versions and (len(versions) != 2 or not agent):
            raise ValueError("--compare needs exactly STABLE,CANDIDATE and --agent")
        if args or not (tenant and since and until):
            raise ValueError("missing or unexpected arguments")
        group = tuple(k for k in by.split(",") if k)
        unknown = set(group) - set(get_args(GroupBy))
        if unknown:
            raise ValueError(f"unknown --by key(s): {sorted(unknown)}")
        query = UsageQuery(
            tenant_id=tenant,
            since=_day(since),
            until=_day(until),
            group_by=group,
            agent_id=agent,
        )
    except (ValueError, ValidationError) as exc:
        print(f"{exc}\n{USAGE}", file=sys.stderr)
        return 2
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    try:
        if versions:
            return asyncio.run(compare(dsn, query, versions, prices))
        return asyncio.run(run(dsn, query, prices, as_json))
    except (OSError, DefinitionError) as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
