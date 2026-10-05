"""The LLM usage ledger on PostgreSQL: the same contract as the in-memory one, append-only in the
database itself, and fed by the REAL engine (purposes, no double counting on replay)."""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest

from conversation_agent.adapters.journal.memory import InMemoryTurnJournal
from conversation_agent.adapters.llm.fake import FakeLLM, text_response, tool_call_response
from conversation_agent.adapters.llm.metered import MeteredLLMProvider
from conversation_agent.adapters.llm.metrics import InMemoryLLMMetrics
from conversation_agent.adapters.llm.usage_memory import InMemoryLLMUsageStore
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.llm_usage import PostgresLLMUsageStore
from conversation_agent.core.llm_prices import PriceTable
from conversation_agent.core.models.conversation import ConversationState
from conversation_agent.core.models.llm import LLMResponse, LLMUsage
from conversation_agent.core.models.llm_usage import UsageQuery
from conversation_agent.core.observability import bind
from conversation_agent.engine.turn_engine import TurnEngine
from observability.test_llm_usage import JAN, call, contract, query, seed, table
from support.builders import IDENTITY, new_clock
from support.support_domain import compiled_support, pipeline_for


async def test_the_durable_ledger_follows_the_same_contract(db: PostgresDatabase) -> None:
    await contract(PostgresLLMUsageStore(db))


async def test_both_ledgers_report_exactly_the_same_numbers(db: PostgresDatabase) -> None:
    pg, memory = PostgresLLMUsageStore(db), InMemoryLLMUsageStore()
    await seed(pg)
    await seed(memory)
    for group_by in ((), ("purpose",), ("agent_version", "model"), ("day", "purpose")):
        a = await pg.report(query(group_by=group_by), table())
        b = await memory.report(query(group_by=group_by), table())

        def key(r: Any) -> tuple[Any, ...]:
            return (*sorted((k, str(v)) for k, v in r.keys.items()), str(r.model))

        for x, y in zip(sorted(a, key=key), sorted(b, key=key), strict=True):
            assert x.keys == y.keys and x.calls == y.calls and x.errors == y.errors
            assert x.input_tokens == y.input_tokens and x.cost_usd == pytest.approx(y.cost_usd)
            assert x.latency_p95_ms == pytest.approx(y.latency_p95_ms)
        assert len(a) == len(b)


async def test_the_ledger_is_append_only_in_the_database_itself(db: PostgresDatabase) -> None:
    await PostgresLLMUsageStore(db).record(call())
    with pytest.raises(asyncpg.PostgresError, match="append-only"):
        await db.pool.execute("UPDATE llm_usage SET input_tokens = 0")
    with pytest.raises(asyncpg.PostgresError, match="append-only"):
        await db.pool.execute("DELETE FROM llm_usage")


async def test_the_ledger_holds_no_prompt_or_answer_column(db: PostgresDatabase) -> None:
    columns = {
        r["column_name"]
        for r in await db.pool.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'llm_usage'"
        )
    }
    assert not columns & {"prompt", "system", "messages", "text", "content", "response"}


# --- fed by the real engine ---


def priced_answer(text: str = "ok") -> LLMResponse:
    return text_response(text).model_copy(
        update={
            "provider": "anthropic",
            "model": "claude-x",
            "usage": LLMUsage(input_tokens=1000, output_tokens=200),
        }
    )


def engine_for(llm: object, mcp: object, journal: InMemoryTurnJournal | None = None) -> TurnEngine:
    compiled = compiled_support()
    return TurnEngine(
        compiled,
        llm,  # type: ignore[arg-type]
        pipeline_for(compiled, mcp),  # type: ignore[arg-type]
        journal or InMemoryTurnJournal(),
        new_clock(),
    )


async def test_a_replayed_turn_never_counts_a_call_twice_and_the_books_name_the_agent(
    db: PostgresDatabase, mcp: object
) -> None:
    store = PostgresLLMUsageStore(db)
    metered = MeteredLLMProvider(
        FakeLLM(
            [
                tool_call_response("support__search_faq", {"question": "horário"}),
                priced_answer("Atendemos de segunda a sexta."),
            ]
        ),
        store,
        metrics=InMemoryLLMMetrics(),
        prices=PriceTable(),
    )
    journal = InMemoryTurnJournal()
    with bind(tenant_id=IDENTITY.tenant_id, agent_id="support-demo", agent_version="0.1.0"):
        first = await engine_for(metered, mcp, journal).process_turn(
            IDENTITY, ConversationState(), "Que horário vocês atendem?", "turn-1"
        )
        # a restart: a NEW engine over the SAME journal re-runs the turn from the stored steps
        again = await engine_for(metered, mcp, journal).process_turn(
            IDENTITY, ConversationState(), "Que horário vocês atendem?", "turn-1"
        )
    assert again.reply == first.reply == "Atendemos de segunda a sexta."
    rows = await db.pool.fetch("SELECT purpose, agent_id, agent_version, model FROM llm_usage")
    assert len(rows) == 2  # the tool-use call and the answer: the replay added NOTHING
    assert {r["purpose"] for r in rows} == {"agent"}
    assert {(r["agent_id"], r["agent_version"]) for r in rows} == {("support-demo", "0.1.0")}


async def test_flow_understanding_and_digression_calls_carry_their_own_purpose(
    db: PostgresDatabase, mcp: object
) -> None:
    from conversation_agent.core.models.llm import LLMStopReason

    understood = LLMResponse(
        parts=(),
        stop_reason=LLMStopReason.END_TURN,
        usage=LLMUsage(input_tokens=10, output_tokens=5),
        structured={"kind": "digression"},
    )
    store = PostgresLLMUsageStore(db)
    llm = MeteredLLMProvider(
        FakeLLM([understood, priced_answer("A consulta custa R$ 150.")]), store
    )
    engine = engine_for(llm, mcp)
    state = ConversationState()
    with bind(tenant_id=IDENTITY.tenant_id):
        out = await engine.process_turn(IDENTITY, state, "Quero abrir um chamado", "turn-1")
        out = await engine.process_turn(IDENTITY, out.state, "Não consigo entrar", "turn-2")
        await engine.process_turn(IDENTITY, out.state, "quanto custa a consulta?", "turn-3")
    rows = await db.pool.fetch("SELECT purpose FROM llm_usage ORDER BY id")
    assert [r["purpose"] for r in rows] == ["flow_understanding", "flow_digression"]
    report = await store.report(
        UsageQuery(tenant_id=IDENTITY.tenant_id, since=JAN.replace(year=2020), until=JAN.replace(year=2100),
                   group_by=("purpose",)),
        PriceTable(),
    )  # fmt: skip
    assert {r.keys["purpose"]: r.calls for r in report} == {
        "flow_understanding": 1,
        "flow_digression": 1,
    }


async def test_the_usage_cli_prints_a_priced_report_and_flags_the_gap(
    db: PostgresDatabase,
    pg_dsn: str,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import asyncio

    from conversation_agent.app.usage import main

    await seed(PostgresLLMUsageStore(db))
    prices = tmp_path / "prices.yaml"
    prices.write_text(
        "models:\n  anthropic/claude-x:\n    - {effective_from: 2026-01-01, input_per_mtok: 3, output_per_mtok: 15}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABASE_URL", pg_dsn)
    loop = asyncio.get_running_loop()

    async def cli(*args: str) -> int:
        return await loop.run_in_executor(None, main, list(args))

    window = ["--tenant", "t1", "--since", "2026-01-01", "--until", "2026-03-01"]
    assert await cli(*window, "--by", "purpose", "--prices", str(prices)) == 0
    out = capsys.readouterr().out
    assert "confirmation_decision" in out and "lower bound" in out  # the unpriced model is flagged
    assert await cli(*window, "--json", "--prices", str(prices)) == 0
    assert '"calls": 5' in capsys.readouterr().out
    assert await cli("--tenant", "t1") == 2 and await cli(*window, "--by", "color") == 2
    monkeypatch.delenv("DATABASE_URL")
    assert await cli(*window) == 2


# --- comparing versions per turn (what a canary asks) ---


def turn_call(
    version: str, turn: int, *, calls: int = 1, tokens: int = 1_000_000, **kw: Any
) -> list[Any]:
    return [
        call(
            agent_version=version,
            turn_id=f"{version}-turn-{turn}",
            conversation_ref=f"conv-{version}-{turn % 5}",
            input_tokens=tokens,
            output_tokens=0,
            **kw,
        )
        for _ in range(calls)
    ]


async def seed_versions(store: PostgresLLMUsageStore) -> None:
    for turn in range(40):  # stable: one call per turn
        for record in turn_call("1.0.0", turn):
            await store.record(record)
    for turn in range(40):  # candidate: three calls per turn (a chattier agent)
        for record in turn_call("1.1.0", turn, calls=3):
            await store.record(record)


async def test_versions_are_compared_per_turn_not_by_total_spend(db: PostgresDatabase) -> None:
    from conversation_agent.core.llm_compare import cost_regressions

    store = PostgresLLMUsageStore(db)
    await seed_versions(store)
    q = query()
    stable, candidate = await store.compare_versions(
        "t1", "support", ["1.0.0", "1.1.0"], q.since, q.until, table()
    )
    assert (stable.turns, candidate.turns) == (40, 40) and stable.conversations == 5
    assert (stable.calls_per_turn, candidate.calls_per_turn) == (1.0, 3.0)
    assert candidate.cost_per_turn == pytest.approx(stable.cost_per_turn * 3)
    problems = cost_regressions(stable, candidate)
    assert any("cost per turn" in p for p in problems) and any(
        "model calls per turn" in p for p in problems
    )
    assert cost_regressions(stable, stable) == []  # a version is never worse than itself
    (nothing,) = await store.compare_versions("t1", "support", ["9.9.9"], q.since, q.until, table())
    assert nothing.turns == 0 and nothing.cost_per_turn == 0.0


def test_a_verdict_needs_enough_turns_and_flags_gaps_and_errors() -> None:
    from conversation_agent.core.llm_compare import VersionCost, cost_regressions

    few = VersionCost(agent_version="1.1.0", turns=5, calls=5, cost_usd=1.0)
    full = VersionCost(agent_version="1.0.0", turns=100, calls=100, cost_usd=10.0)
    assert any("only 5 turns" in p for p in cost_regressions(full, few))
    unpriced = full.model_copy(update={"agent_version": "1.1.0", "unpriced_calls": 3})
    assert any("lower bound" in p for p in cost_regressions(full, unpriced))
    erroring = full.model_copy(update={"agent_version": "1.1.0", "errors": 30})
    assert any("error rate" in p for p in cost_regressions(full, erroring))
    assert (
        cost_regressions(full, full.model_copy(update={"agent_version": "1.1.0", "cost_usd": 11.0}))
        == []
    )


async def test_the_compare_cli_exits_one_when_the_candidate_is_worse(
    db: PostgresDatabase,
    pg_dsn: str,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import asyncio

    from conversation_agent.app.usage import main

    await seed_versions(PostgresLLMUsageStore(db))
    prices = tmp_path / "p.yaml"
    prices.write_text(
        "models:\n  anthropic/claude-x:\n    - {effective_from: 2026-01-01, input_per_mtok: 3, output_per_mtok: 15}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABASE_URL", pg_dsn)
    loop = asyncio.get_running_loop()

    async def cli(*args: str) -> int:
        return await loop.run_in_executor(None, main, list(args))

    window = [
        "--tenant",
        "t1",
        "--agent",
        "support",
        "--since",
        "2026-01-01",
        "--until",
        "2026-03-01",
    ]
    assert await cli(*window, "--compare", "1.0.0,1.1.0", "--prices", str(prices)) == 1
    out = capsys.readouterr().out
    assert "candidate looks worse" in out and "model calls per turn" in out
    assert await cli(*window, "--compare", "1.0.0,1.0.0", "--prices", str(prices)) == 0
    assert "no regression found" in capsys.readouterr().out
    assert await cli(*window, "--compare", "1.0.0") == 2  # needs STABLE,CANDIDATE


async def test_the_durable_ledger_keeps_the_seconds_of_audio_and_prices_the_minutes(
    db: PostgresDatabase,
) -> None:
    from datetime import UTC, date, datetime

    from conversation_agent.core.llm_prices import ModelPrice
    from conversation_agent.core.models.llm_usage import LLMCallRecord

    store = PostgresLLMUsageStore(db)
    at = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    for seconds in (30, 90):
        await store.record(
            LLMCallRecord(
                tenant_id="t1",
                started_at=at,
                purpose="transcription",
                provider="groq",
                model="whisper-large-v3-turbo",
                audio_seconds=seconds,
            )
        )
    prices = PriceTable(
        models={
            "groq/whisper-large-v3-turbo": (
                ModelPrice(
                    effective_from=date(2026, 1, 1),
                    input_per_mtok=0,
                    output_per_mtok=0,
                    audio_per_minute=0.0006,
                ),
            )
        }
    )
    (row,) = await store.report(
        UsageQuery(
            tenant_id="t1",
            since=datetime(2026, 10, 1, tzinfo=UTC),
            until=datetime(2026, 11, 1, tzinfo=UTC),
            group_by=("purpose",),
        ),
        prices,
    )
    assert row.audio_seconds == 120 and row.calls == 2
    assert row.cost_usd == pytest.approx(0.0012) and row.unpriced_calls == 0
    with pytest.raises(asyncpg.PostgresError):  # a negative duration is refused by the database too
        await db.pool.execute("UPDATE llm_usage SET audio_seconds = -1")  # (append-only anyway)
