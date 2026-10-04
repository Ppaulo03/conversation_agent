"""INV-012 / RUNTIME_PROTOCOL §10: cancellation is cooperative and only acts at safe boundaries.

C12: `cancel_requested` while a ToolProvider call is in flight never interrupts it: the call
finishes, the ledger is finalized and applied, and the conversation stops at the next boundary.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from conftest import ApiHandle
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response, tool_call_response
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.inbox import PostgresInboxStore
from conversation_agent.core.models.runtime import InvocationStatus
from postgres.world import KEY, AllowWrites, World, event
from support.builders import availability_call

BOOKING = {
    "service_id": "haircut",
    "start_at": "2026-10-06T10:00:00-03:00",
    "duration_minutes": 30,
}
POLICY = AllowWrites(frozenset({"scheduling.availability", "scheduling.create"}))


class GatedProvider:
    """Holds every external call until released: the call is 'in flight'."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.finished = 0

    async def execute(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        self.started.set()
        await self.release.wait()
        result = await self._inner.execute(*args, **kwargs)
        self.finished += 1
        return result


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


def restart_policy(world: World) -> None:
    world.inbox = PostgresInboxStore(world.db, world.clock, restart_on_new_message=True)


async def flag(world: World) -> bool:
    return bool(await world.db.pool.fetchval("SELECT cancel_requested FROM conversation_states"))


async def wait_for_flag(world: World, expected: bool = True) -> None:
    for _ in range(100):
        if await flag(world) is expected:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"cancel_requested never became {expected}")


def coordinator(world: World, gate: GatedProvider, llm: FakeLLM) -> Any:
    return world.coordinator(
        "w1",
        llm,
        providers={"http": gate},
        policy=POLICY,
        heartbeat_interval_seconds=0.05,  # the worker notices the request quickly
    )


async def test_C12_cancel_during_external_call_finishes_call_then_stops_at_boundary(
    world: World, api: ApiHandle
) -> None:
    restart_policy(world)
    gate = GatedProvider(world.http_provider(api.base_url))
    llm = FakeLLM(
        [
            tool_call_response("scheduling__create", BOOKING),
            text_response("Certo, vamos ver o novo pedido."),
        ]
    )
    await world.inbox.insert_if_absent(event("e1", "quero terça 10h", clock=world.clock))
    task = asyncio.create_task(coordinator(world, gate, llm).process_conversation(KEY))
    await asyncio.wait_for(gate.started.wait(), 5)  # the write is inside the external call

    await world.inbox.insert_if_absent(event("e2", "na verdade quero outro", clock=world.clock))
    await wait_for_flag(world)  # cancel_requested is set...
    await asyncio.sleep(0.3)  # ...and the worker has seen it (heartbeat), still waiting on I/O
    assert gate.finished == 0 and len(api.state.bookings) == 0  # the call was NOT interrupted

    gate.release.set()
    run = await asyncio.wait_for(task, 10)
    assert run.status == "done" and run.turns_completed == 2  # the newer message ran after

    assert gate.finished == 1 and len(api.state.bookings) == 1  # the call completed
    invocation = await world.db.pool.fetchrow(
        "SELECT status, result_application_status FROM tool_invocations"
    )
    assert invocation is not None
    assert invocation["status"] == InvocationStatus.SUCCEEDED.value  # ledger finalized (C1)
    assert invocation["result_application_status"] == "applied"  # and applied (C2)
    texts = [
        r["text"]
        for r in await world.db.pool.fetch(
            "SELECT text FROM outbox_messages ORDER BY created_at, message_index"
        )
    ]
    assert "concluído" in texts[0]  # the real effect is reported, not silently dropped
    assert texts[-1] == "Certo, vamos ver o novo pedido."
    assert llm.calls == 2  # no model call between the booking and the stop
    assert await world.count("turns", "status='COMPLETED'") == 2
    assert await flag(world) is False


async def test_restart_before_any_irreversible_step_abandons_the_turn_and_merges_messages(
    world: World, api: ApiHandle
) -> None:
    restart_policy(world)
    gate = GatedProvider(world.http_provider(api.base_url))
    llm = FakeLLM(
        [
            availability_call("haircut", "2026-10-06"),
            text_response("(abandoned turn: never used)"),
            text_response("Ok, sobre o corte de cabelo na quarta."),
        ]
    )
    await world.inbox.insert_if_absent(event("e1", "quero terça", clock=world.clock))
    task = asyncio.create_task(coordinator(world, gate, llm).process_conversation(KEY))
    await asyncio.wait_for(gate.started.wait(), 5)  # a READ is in flight (nothing irreversible)
    await world.inbox.insert_if_absent(event("e2", "melhor quarta", clock=world.clock))
    await wait_for_flag(world)
    await asyncio.sleep(0.3)
    gate.release.set()
    run = await asyncio.wait_for(task, 10)

    assert run.status == "done" and run.turns_completed == 1
    assert await world.count("turns", "status='CANCELLED'") == 1
    assert await world.count("turns", "status='COMPLETED'") == 1
    text = await world.db.pool.fetchval("SELECT user_text FROM turns WHERE status='COMPLETED'")
    assert "quero terça" in text and "melhor quarta" in text  # both messages, one answer
    assert await world.count("outbox_messages") == 1
    assert await world.count("inbox_events", "status='CONSUMED'") == 2
    assert api.state.bookings == {}


async def test_default_policy_queues_a_message_that_arrives_mid_turn(
    world: World, api: ApiHandle
) -> None:
    gate = GatedProvider(world.http_provider(api.base_url))
    llm = FakeLLM(
        [
            availability_call("haircut", "2026-10-06"),
            text_response("Primeira resposta."),
            text_response("Segunda resposta."),
        ]
    )
    await world.inbox.insert_if_absent(event("e1", "quero terça", clock=world.clock))
    task = asyncio.create_task(coordinator(world, gate, llm).process_conversation(KEY))
    await asyncio.wait_for(gate.started.wait(), 5)
    await world.inbox.insert_if_absent(event("e2", "e quarta?", clock=world.clock))
    await asyncio.sleep(0.2)
    assert await flag(world) is False  # `queue`: nothing asks the turn to stop
    gate.release.set()
    run = await asyncio.wait_for(task, 10)
    assert run.turns_completed == 2 and await world.count("turns", "status='CANCELLED'") == 0
