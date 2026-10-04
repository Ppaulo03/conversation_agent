"""The Phase 1 TurnEngine running on the durable, fenced PostgreSQL journal.

C10_after_llm_response, C15_journal_hash_divergence, INV-014/017/018 over real storage; a
"restart" is a brand-new pool + a new lease epoch (the old process is gone).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.postgres.coordination import FixedCoordinationTime
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.lease import PostgresLeaseStore
from conversation_agent.adapters.postgres.uow import PostgresTurnJournal, PostgresUnitOfWorkFactory
from conversation_agent.adapters.tools.fake import FakeToolProvider
from conversation_agent.core.errors import (
    FencingError,
    JournalConflictError,
    JournalDivergenceError,
    LLMProviderError,
)
from conversation_agent.core.models.conversation import ConversationState
from conversation_agent.core.models.journal import JournalEntry, JournalStepType
from conversation_agent.core.models.runtime import ConversationKey
from conversation_agent.core.models.tooling import ToolResult
from conversation_agent.engine.turn_engine import TurnEngine
from support.builders import IDENTITY, availability_call
from vertical_slice.wiring import build_engine

TTL = timedelta(seconds=30)
TURN = "turn-durable-1"
KEY = ConversationKey(tenant_id=IDENTITY.tenant_id, conversation_id=IDENTITY.conversation_id)
SLOTS = ToolResult(status="success", data={"items": [], "pagination": {"next_cursor": None}})


class Worker:
    """A 'process': its own pool, lease and fenced journal."""

    def __init__(self, db: PostgresDatabase, clock: FixedClock, name: str) -> None:
        self.db, self.clock, self.name = db, clock, name
        self.tools = FakeToolProvider({"erp_get_available_slots": SLOTS})

    async def start(self) -> Worker:
        lease = await PostgresLeaseStore(
            self.db, self.clock, FixedCoordinationTime(self.clock)
        ).acquire(KEY, self.name, TTL)
        assert lease is not None, "lease not acquired"
        self.lease = lease
        factory = PostgresUnitOfWorkFactory(self.db, self.clock, FixedCoordinationTime(self.clock))
        self.journal = PostgresTurnJournal(factory, self.db, lease.fence)
        return self

    def engine(self, llm: FakeLLM) -> TurnEngine:
        engine, _, _ = build_engine(
            llm,
            api_base_url="http://unused",
            journal=self.journal,
            clock=self.clock,
            providers={"http": self.tools},
        )
        return engine

    async def say(self, llm: FakeLLM, text: str = "quero terça") -> str:
        outcome = await self.engine(llm).process_turn(IDENTITY, ConversationState(), text, TURN)
        return outcome.reply


async def test_C10_after_llm_response_restart_replays_journal_without_new_llm_or_tool_call(
    db: PostgresDatabase, clock: FixedClock, conversation: object, pg_dsn: str
) -> None:
    first = await Worker(db, clock, "worker-a").start()
    crashing = FakeLLM([availability_call("haircut", "2026-10-06"), LLMProviderError("killed")])
    with pytest.raises(LLMProviderError):
        await first.say(crashing)
    assert crashing.calls == 2 and len(first.tools.calls) == 1

    await db.close()  # the process dies...
    clock.set(clock.now() + TTL + timedelta(seconds=1))  # ...its lease expires
    reborn_db = await PostgresDatabase.connect(pg_dsn)
    try:
        second = await Worker(reborn_db, clock, "worker-b").start()
        assert second.lease.epoch == 2
        resumed = FakeLLM([text_response("Tenho horários na terça.")])  # only the *new* step
        reply = await second.say(resumed)
    finally:
        await reborn_db.close()

    assert reply == "Tenho horários na terça."
    assert resumed.calls == 1  # LLM step 1 replayed from PostgreSQL, no new charge
    assert second.tools.calls == []  # tool result replayed too; the tool was not run again


async def test_completed_turn_replays_with_zero_external_calls(
    db: PostgresDatabase, clock: FixedClock, conversation: object
) -> None:
    worker = await Worker(db, clock, "worker-a").start()
    first = await worker.say(
        FakeLLM([availability_call("haircut", "2026-10-06"), text_response("Tenho 09:00.")])
    )
    silent = FakeLLM([])
    again = await worker.say(silent)
    assert again == first and silent.calls == 0 and len(worker.tools.calls) == 1


async def test_C15_journal_hash_divergence_fails_closed_on_postgres(
    db: PostgresDatabase, clock: FixedClock, conversation: object
) -> None:
    worker = await Worker(db, clock, "worker-a").start()
    await worker.say(FakeLLM([text_response("oi")]), text="mensagem A")
    llm = FakeLLM([text_response("não pode ser chamado")])
    with pytest.raises(JournalDivergenceError):
        await worker.say(llm, text="mensagem B")
    assert llm.calls == 0 and worker.tools.calls == []


async def test_replay_reuses_turn_reference_time_across_restarts(
    db: PostgresDatabase, clock: FixedClock, conversation: object
) -> None:
    worker = await Worker(db, clock, "worker-a").start()
    first = FakeLLM([availability_call("haircut", "2026-10-06"), LLMProviderError("killed")])
    with pytest.raises(LLMProviderError):
        await worker.say(first)

    clock.set(clock.now() + TTL + timedelta(days=1))  # "now" moved a day later
    second = await Worker(db, clock, "worker-b").start()
    resumed = FakeLLM([text_response("ok")])
    await second.say(resumed)
    assert resumed.requests[0].system == first.requests[0].system  # same "now" in the prompt


async def test_journal_is_unique_per_step_and_fenced(
    db: PostgresDatabase, clock: FixedClock, conversation: object
) -> None:
    worker = await Worker(db, clock, "worker-a").start()
    e = JournalEntry(turn_id="t", step_index=0, step_type=JournalStepType.INBOUND_AGGREGATED)
    await worker.journal.append(e)
    with pytest.raises(JournalConflictError):
        await worker.journal.append(e)

    clock.set(clock.now() + TTL + timedelta(seconds=1))
    await Worker(db, clock, "worker-b").start()  # takeover
    with pytest.raises(FencingError):  # the old worker's journal handle is now dead
        await worker.journal.append(e.model_copy(update={"step_index": 1}))
    assert [x.step_index for x in await worker.journal.entries("t")] == [0]
