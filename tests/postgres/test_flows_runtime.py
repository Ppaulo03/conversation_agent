"""Phase 4 on the durable runtime: Flows + confirmation + ledger + ownership gate."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from conftest import ApiHandle
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.models.conversation import ConversationState
from postgres.test_protected_actions import (
    answer,
    coord,
    deliver_prompt,
    pending,
    posts,
    statuses,
    structured,
)
from postgres.world import KEY, World, event


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


async def say(world: World, api: ApiHandle, text: str, llm: Any | None = None) -> Any:
    await world.inbox.insert_if_absent(
        event(f"f-{world.clock.now().isoformat()}-{text[:6]}", text, clock=world.clock)
    )
    return await coord(world, api, "flow", llm or FakeLLM([]), flows=True).process_conversation(KEY)


async def stored_flows(world: World) -> ConversationState:
    row = await world.db.pool.fetchrow("SELECT state_json FROM conversation_states")
    assert row is not None
    raw = row["state_json"]
    return ConversationState.model_validate(json.loads(raw) if isinstance(raw, str) else raw)


async def propose_via_flow(world: World, api: ApiHandle) -> None:
    llm = FakeLLM([])
    run = await say(world, api, "Quero marcar um corte amanhã às 10h", llm)
    assert run.status == "done" and llm.calls == 0  # the Flow answered; no model involved
    await deliver_prompt(world)


async def test_flow_proposal_goes_through_the_protected_action_machinery(
    world: World, api: ApiHandle
) -> None:
    await propose_via_flow(world, api)
    action = await pending(world)
    assert action["status"] == "PENDING_CONFIRMATION" and posts(api) == []  # nothing executed
    state = await stored_flows(world)
    assert state.active_flow is not None  # persisted: the flow survives between turns
    assert state.active_flow.slots["service"] == "haircut"


async def test_flow_closes_when_the_confirmed_action_executes(world: World, api: ApiHandle) -> None:
    await propose_via_flow(world, api)
    llm = FakeLLM([text_response("Agendado para terça às 10h!")])
    run = await answer(world, api, "sim", llm, flows=True)
    assert run.status == "done" and len(posts(api)) == 1
    assert (await stored_flows(world)).flows == ()  # done: nothing left open


async def test_flow_closes_when_the_user_rejects(world: World, api: ApiHandle) -> None:
    await propose_via_flow(world, api)
    await answer(world, api, "não", FakeLLM([]), flows=True)
    assert posts(api) == [] and (await stored_flows(world)).flows == ()
    assert await statuses(world) == ["REJECTED"]


async def test_changing_the_request_instead_of_answering_corrects_the_flow(
    world: World, api: ApiHandle
) -> None:
    await propose_via_flow(world, api)
    llm = FakeLLM([structured("modify", 0.95)])
    run = await answer(world, api, "na verdade às 11h", llm, flows=True)
    assert run.status == "done" and posts(api) == []
    state = await stored_flows(world)
    assert state.active_flow is not None and state.active_flow.slots["preferred_time"] == "11:00"
    assert state.active_flow.slots["service"] == "haircut"  # nothing else was lost
    assert await statuses(world) == ["INVALIDATED", "PENDING_CONFIRMATION"]


async def test_a_conflict_at_execution_follows_the_flow_transition(
    world: World, api: ApiHandle
) -> None:
    await propose_via_flow(world, api)
    api.state.taken_slots = {datetime.fromisoformat("2026-10-06T10:00:00-03:00")}
    await answer(world, api, "sim", FakeLLM([text_response("Não consegui.")]), flows=True)
    state = await stored_flows(world)
    assert state.active_flow is not None  # still open: it asks for another day
    assert "date" not in state.active_flow.slots
    assert state.active_flow.slots["service"] == "haircut"
    message = await world.db.pool.fetchval(
        "SELECT text FROM outbox_messages ORDER BY created_at DESC"
    )
    assert "acabou de ser ocupado" in message and "Para qual dia" in message


async def test_human_owned_conversation_never_runs_flow_router_or_llm(  # INV-019
    world: World, api: ApiHandle
) -> None:
    await world.inbox.insert_if_absent(
        event("h-1", "Quero marcar um corte amanhã às 10h", clock=world.clock)
    )
    await world.db.pool.execute("UPDATE conversation_states SET ownership='HUMAN'")
    llm = FakeLLM([])
    await coord(world, api, "flow", llm, flows=True).process_conversation(KEY)
    assert llm.calls == 0 and api.requests == []  # no Router, no LLM, no tool
    assert await world.count("outbox_messages") == 0
    assert (await stored_flows(world)).flows == ()  # context kept, flow NOT started
