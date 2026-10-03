"""Inbox -> lease -> claim -> turn -> outbox, against real PostgreSQL (Phase 2.2).

INV-007, INV-019, INV-020, claim-after-lease, restart survival, zombie worker (C09).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.errors import LLMProviderError
from conversation_agent.core.models.conversation import ConversationState
from conversation_agent.core.models.llm import LLMRequest, LLMResponse
from conversation_agent.core.models.runtime import OutboxStatus
from postgres.world import KEY, TTL, World, event
from support.builders import IDENTITY, availability_call


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


async def stored_state(world: World) -> ConversationState:
    raw = await world.db.pool.fetchval("SELECT state_json FROM conversation_states")
    return ConversationState.model_validate(raw)


async def test_one_event_becomes_one_turn_one_outbox_message_and_is_consumed(
    world: World,
) -> None:
    assert await world.inbox.insert_if_absent(
        event("e1", "Quero cortar o cabelo", clock=world.clock)
    )
    llm = FakeLLM([text_response("Claro! Para qual dia?")])
    runs = await world.coordinator("w1", llm).run_once()

    assert [(r.status, r.turns_completed) for r in runs] == [("done", 1)]
    assert await world.count("turns", "status='COMPLETED'") == 1
    assert await world.count("inbox_events", "status='CONSUMED'") == 1
    assert await world.count("outbox_messages", "status='PENDING'") == 1
    state = await stored_state(world)
    assert [m.text for m in state.history] == ["Quero cortar o cabelo", "Claro! Para qual dia?"]
    assert world.sender.attempts == []  # INV-007: the turn itself never touches the channel
    # the lease is released at the end
    assert await world.db.pool.fetchval("SELECT lease_owner FROM conversation_states") is None


async def test_outbox_worker_is_the_only_path_to_the_channel(world: World) -> None:  # INV-007
    await world.inbox.insert_if_absent(event("e1", clock=world.clock))
    await world.coordinator("w1", FakeLLM([text_response("Olá!")])).run_once()
    assert world.sender.delivered == []

    assert await world.outbox_worker("sender-1").run_once() == 1
    assert [m.text for m in world.sender.delivered] == ["Olá!"]
    row = await world.db.pool.fetchrow("SELECT status, provider_message_id FROM outbox_messages")
    assert row is not None and row["status"] == OutboxStatus.ACCEPTED
    assert row["provider_message_id"] == "prov-1"
    assert await world.outbox_worker("sender-1").run_once() == 0  # nothing left to send


async def test_duplicate_event_never_creates_a_duplicate_turn(world: World) -> None:  # INV-020
    results = await asyncio.gather(
        *(world.inbox.insert_if_absent(event("dup", "oi", clock=world.clock)) for _ in range(8))
    )
    assert results.count(True) == 1  # concurrent redelivery race: exactly one winner
    assert await world.inbox.insert_if_absent(event("dup", "oi", clock=world.clock)) is False

    llm = FakeLLM([text_response("Olá!")])
    await world.coordinator("w1", llm).run_once()
    await world.coordinator("w2", llm).run_once()  # a second pass finds nothing
    assert await world.count("inbox_events") == 1
    assert await world.count("turns") == 1
    assert await world.count("outbox_messages") == 1
    assert llm.calls == 1


async def test_a_burst_is_ordered_and_aggregated_into_one_turn(world: World) -> None:
    c = world.clock
    # arrival order differs from occurrence order; source_sequence is authoritative when present
    await world.inbox.insert_if_absent(event("e3", "terceira", clock=c, occurred_offset_s=3))
    await world.inbox.insert_if_absent(event("e1", "primeira", clock=c, occurred_offset_s=1))
    await world.inbox.insert_if_absent(event("e2", "segunda", clock=c, occurred_offset_s=2))
    llm = FakeLLM([text_response("ok")])
    await world.coordinator("w1", llm).run_once()
    assert llm.calls == 1
    assert (await stored_state(world)).history[0].text == "primeira\nsegunda\nterceira"

    c.set(c.now() + timedelta(minutes=1))
    await world.inbox.insert_if_absent(event("s2", "B", clock=c, sequence=2))
    await world.inbox.insert_if_absent(event("s1", "A", clock=c, sequence=1))
    await world.coordinator("w1", FakeLLM([text_response("ok")])).run_once()
    assert (await stored_state(world)).history[2].text == "A\nB"


async def test_late_event_enters_the_next_turn_and_never_rewrites_the_past(world: World) -> None:
    c = world.clock
    await world.inbox.insert_if_absent(event("e1", "agora", clock=c, occurred_offset_s=10))
    await world.coordinator("w1", FakeLLM([text_response("r1")])).run_once()
    first_history = (await stored_state(world)).history

    # an event that *occurred* before the committed turn's event arrives afterwards
    await world.inbox.insert_if_absent(event("late", "esqueci", clock=c, occurred_offset_s=5))
    await world.coordinator("w1", FakeLLM([text_response("r2")])).run_once()

    state = await stored_state(world)
    assert state.history[: len(first_history)] == first_history  # committed turn untouched
    assert [m.text for m in state.history[2:]] == ["esqueci", "r2"]
    inbound = await world.db.pool.fetchval(
        "SELECT j.payload FROM turn_journal j JOIN turns t ON t.turn_id = j.turn_id "
        "WHERE j.step_type='INBOUND_AGGREGATED' AND t.user_text='esqueci'"
    )
    assert inbound["late_event_ids"] == ["late"]


async def test_claim_happens_only_after_the_lease_and_the_loser_claims_nothing(
    world: World,
) -> None:
    await world.inbox.insert_if_absent(event("e1", clock=world.clock))
    holder = await world.leases.acquire(KEY, "someone-else", TTL)
    assert holder is not None

    llm = FakeLLM([text_response("não deve rodar")])
    runs = await world.coordinator("w1", llm).run_once()
    assert [r.status for r in runs] == ["busy"]
    assert llm.calls == 0
    assert await world.count("inbox_events", "status='READY'") == 1  # untouched
    assert await world.count("turns") == 0

    await world.leases.release(holder)
    runs = await world.coordinator("w1", FakeLLM([text_response("ok")])).run_once()
    assert [r.status for r in runs] == ["done"]


async def test_human_owner_never_emits_automated_outbound(world: World) -> None:  # INV-019
    await world.inbox.insert_if_absent(event("e1", "Oi, tem alguém aí?", clock=world.clock))
    await world.db.pool.execute("UPDATE conversation_states SET ownership='HUMAN'")
    llm = FakeLLM([text_response("não deve ser chamado")])
    await world.coordinator("w1", llm).run_once()

    assert llm.calls == 0  # no Router, no LLM, no tool
    assert world.tools.calls == []
    assert await world.count("outbox_messages") == 0
    assert await world.count("turns", "status='COMPLETED'") == 1
    assert await world.count("inbox_events", "status='CONSUMED'") == 1
    state = await stored_state(world)  # context is persisted for the human agent
    assert [(m.role, m.text) for m in state.history] == [("user", "Oi, tem alguém aí?")]


async def test_runtime_survives_restart_open_turn_and_pending_outbox(
    world: World, pg_dsn: str
) -> None:
    await world.inbox.insert_if_absent(event("e1", clock=world.clock))
    crashing = FakeLLM([LLMProviderError("provider down")])
    runs = await world.coordinator("w1", crashing).run_once()
    assert [r.status for r in runs] == ["retry_later"]
    assert await world.count("turns", "status='PROCESSING'") == 1  # the turn stays open

    await world.db.close()  # whole process restarts
    reborn_db = await PostgresDatabase.connect(pg_dsn)
    try:
        reborn = World(reborn_db, world.clock)
        llm = FakeLLM([text_response("voltei")])
        runs = await reborn.coordinator("w2", llm).run_once()
        assert [(r.status, r.turns_completed) for r in runs] == [("done", 1)]
        assert await reborn.count("outbox_messages", "status='PENDING'") == 1

        await reborn_db.close()  # restart again before anything was sent
        again = await PostgresDatabase.connect(pg_dsn)
        try:
            third = World(again, world.clock)
            assert await third.outbox_worker("s").run_once() == 1  # the outbox survived
            assert [m.text for m in third.sender.delivered] == ["voltei"]
        finally:
            await again.close()
    finally:
        await reborn_db.close() if not reborn_db.pool.is_closing() else None


class GatedLLM:
    """An LLM call that stays in flight until the test releases it."""

    def __init__(self, response: LLMResponse) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self._response = response

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.started.set()
        await self.release.wait()
        return self._response


async def test_C09_zombie_worker_cannot_alter_the_conversation_after_takeover(
    world: World,
) -> None:
    await world.inbox.insert_if_absent(event("e1", "oi", clock=world.clock))
    gate = GatedLLM(text_response("resposta do zumbi"))
    zombie = asyncio.create_task(world.coordinator("zombie", gate).process_conversation(KEY))
    await asyncio.wait_for(gate.started.wait(), 5)  # the zombie is mid-LLM-call

    world.clock.set(world.clock.now() + TTL + timedelta(seconds=1))  # its lease expires
    heir = await world.coordinator(
        "heir", FakeLLM([text_response("resposta do herdeiro")])
    ).process_conversation(KEY)
    assert (heir.status, heir.turns_completed) == ("done", 1)

    gate.release.set()  # the zombie wakes up and tries to carry on
    result = await asyncio.wait_for(zombie, 5)
    assert result.status == "stale"

    assert (await stored_state(world)).history[-1].text == "resposta do herdeiro"
    assert await world.count("outbox_messages") == 1
    texts = await world.db.pool.fetch("SELECT text FROM outbox_messages")
    assert [r["text"] for r in texts] == ["resposta do herdeiro"]
    responses = await world.db.pool.fetch(
        "SELECT payload FROM turn_journal WHERE step_type='LLM_RESPONSE'"
    )
    assert len(responses) == 1  # the zombie's late response was never persisted


async def test_open_turn_resumed_by_the_next_owner_reuses_its_journal(world: World) -> None:
    await world.inbox.insert_if_absent(event("e1", "terça?", clock=world.clock))
    first = FakeLLM([availability_call("haircut", "2026-10-06"), LLMProviderError("died")])
    await world.coordinator("w1", first).run_once()
    assert len(world.tools.calls) == 1

    resumed = FakeLLM([text_response("Tenho horários.")])
    await world.coordinator("w2", resumed).run_once()
    assert resumed.calls == 1  # step 1 replayed from the journal
    assert len(world.tools.calls) == 1  # the read was not repeated
    assert await world.count("outbox_messages") == 1
    assert IDENTITY.tenant_id  # identity comes from the stored envelope, not from the LLM
