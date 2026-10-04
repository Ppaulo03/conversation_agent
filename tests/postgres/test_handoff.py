"""Human handoff (DoD: HUMAN stays silent). Ownership moves only under the conversation lease,
a Flow can ask for a person, and nothing the bot does survives a change of owner."""

from __future__ import annotations

import pytest

from conftest import ApiHandle
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.definitions.flow import Handoff, Invoke
from conversation_agent.core.models.runtime import Ownership
from conversation_agent.engine.ownership import (
    ConversationBusyError,
    OwnershipService,
    OwnershipTransitionError,
)
from postgres.world import KEY, TTL, World, event
from vertical_slice.definitions import SCHEDULING_FLOW, build_agent


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


def service(world: World, owner: str = "ops") -> OwnershipService:
    return OwnershipService(world.leases, world.uows, owner=owner)


async def start(world: World) -> None:
    await world.inbox.insert_if_absent(event("e0", "oi", clock=world.clock))
    await world.coordinator("c", FakeLLM([text_response("Olá!")])).run_once()


async def owner_of(world: World) -> str:
    return str(await world.db.pool.fetchval("SELECT ownership FROM conversation_states"))


async def test_ownership_moves_through_the_documented_transitions(world: World) -> None:
    await start(world)
    ops = service(world)
    assert await ops.request_handoff(KEY) is Ownership.HANDOFF_PENDING
    assert await ops.assign_human(KEY) is Ownership.HUMAN
    assert await owner_of(world) == "HUMAN"
    assert await ops.return_to_bot(KEY) is Ownership.BOT
    assert await ops.assign_human(KEY) is Ownership.HUMAN  # a person may also take over directly
    assert await ops.assign_human(KEY) is Ownership.HUMAN  # idempotent


async def test_an_invalid_transition_is_refused_and_changes_nothing(world: World) -> None:
    await start(world)
    ops = service(world)
    await ops.assign_human(KEY)
    with pytest.raises(OwnershipTransitionError, match="HUMAN -> HANDOFF_PENDING"):
        await ops.request_handoff(KEY)  # a person already has it: the bot cannot ask again
    assert await owner_of(world) == "HUMAN"


async def test_ownership_cannot_change_underneath_a_turn_in_flight(world: World) -> None:
    await start(world)
    busy = await world.leases.acquire(KEY, "a-turn-is-running", TTL)
    assert busy is not None
    with pytest.raises(ConversationBusyError):
        await service(world).assign_human(KEY)
    assert await owner_of(world) == "BOT"
    await world.leases.release(busy)
    assert await service(world).assign_human(KEY) is Ownership.HUMAN


@pytest.mark.parametrize("owner", ["HANDOFF_PENDING", "HUMAN"])
async def test_a_conversation_the_bot_does_not_own_stores_context_and_says_nothing(
    world: World, owner: str
) -> None:
    await start(world)
    outbound_before = await world.count("outbox_messages")
    ops = service(world)
    await ops.request_handoff(KEY)
    if owner == "HUMAN":
        await ops.assign_human(KEY)
    await world.inbox.insert_if_absent(event("e1", "alguém aí?", clock=world.clock))
    llm = FakeLLM([])
    run = await world.coordinator("c2", llm).run_once()
    assert [r.status for r in run] == ["done"] and llm.calls == 0
    assert await world.count("outbox_messages") == outbound_before  # no outbound, ever
    assert await world.count("tool_invocations") == 0
    state = await world.db.pool.fetchval("SELECT state_json FROM conversation_states")
    assert state["history"][-1] == {"role": "user", "text": "alguém aí?"}  # context is kept


async def test_returning_to_the_bot_resumes_normal_turns(world: World) -> None:
    await start(world)
    ops = service(world)
    await ops.assign_human(KEY)
    await world.inbox.insert_if_absent(event("e1", "ainda estou aqui", clock=world.clock))
    await world.coordinator("c2", FakeLLM([])).run_once()
    await ops.return_to_bot(KEY)
    await world.inbox.insert_if_absent(event("e2", "e agora?", clock=world.clock))
    run = await world.coordinator("c3", FakeLLM([text_response("Voltei!")])).run_once()
    assert [r.status for r in run] == ["done"]
    assert (
        await world.outbox_worker("s").run_once()
    ) == 2  # hello + the answer, nothing from HUMAN


def handoff_agent():  # type: ignore[no-untyped-def]
    """The scheduling flow, but a failing availability search gives up to a person."""
    steps = tuple(
        step.model_copy(update={"default": Handoff(text="Vou chamar um atendente para te ajudar.")})
        if isinstance(step, Invoke)
        else step
        for step in SCHEDULING_FLOW.steps
    )
    flow = SCHEDULING_FLOW.model_copy(update={"steps": steps})
    return build_agent(flows=True).model_copy(update={"flows": (flow,)})


async def test_a_flow_can_hand_the_conversation_to_a_person(world: World, api: ApiHandle) -> None:
    api.state.fault = {"status": 503}  # the schedule cannot be consulted
    await world.inbox.insert_if_absent(
        event("e1", "Quero marcar um corte amanhã às 10h", clock=world.clock)
    )
    llm = FakeLLM([])
    coordinator = world.coordinator(
        "c",
        llm,
        flows=True,
        agent=handoff_agent(),
        providers={"http": world.http_provider(api.base_url)},
    )
    assert [r.status for r in await coordinator.run_once()] == ["done"]
    assert llm.calls == 0
    texts = [r["text"] for r in await world.db.pool.fetch("SELECT text FROM outbox_messages")]
    assert texts == ["Vou chamar um atendente para te ajudar."]  # said first, THEN silence
    assert await owner_of(world) == "HANDOFF_PENDING"
    state = await world.db.pool.fetchval("SELECT state_json FROM conversation_states")
    assert state["flows"] == []  # the flow is over

    api.state.fault = None
    await world.inbox.insert_if_absent(event("e2", "então, e meu horário?", clock=world.clock))
    again = await world.coordinator(
        "c2",
        FakeLLM([]),
        flows=True,
        agent=handoff_agent(),
        providers={"http": world.http_provider(api.base_url)},
    ).run_once()
    assert [r.status for r in again] == ["done"]
    assert await world.count("outbox_messages") == 1  # the bot says nothing more
    assert len(api.availability_requests()) <= 1  # the second message never searched anything
