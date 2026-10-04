"""Phase 12 on the durable runtime: asking for a person really moves the conversation to a person
and the bot goes quiet; an agent with nobody behind it answers honestly and changes nothing."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from conftest import McpHandle
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.compiler import CompiledAgent, compile_manifest
from conversation_agent.engine.ownership import OwnershipService
from postgres.world import KEY, World, event
from support.support_domain import compiled_support, manifest, pipeline_for


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


async def say(
    world: World, mcp: McpHandle, compiled: CompiledAgent, text: str, llm: Any = None
) -> Any:
    world.clock.set(world.clock.now() + timedelta(seconds=5))
    await world.inbox.insert_if_absent(
        event(f"e-{world.clock.now().isoformat()}", text, clock=world.clock)
    )
    coordinator = world.coordinator(
        "w", llm or FakeLLM([]), pipeline=pipeline_for(compiled, mcp), flows=True
    )
    return await coordinator.process_conversation(KEY)


async def owner(world: World) -> str:
    return str(await world.db.pool.fetchval("SELECT ownership FROM conversation_states"))


async def replies(world: World) -> list[str]:
    rows = await world.db.pool.fetch("SELECT text FROM outbox_messages ORDER BY created_at")
    return [r["text"] for r in rows]


async def test_asking_for_a_person_hands_over_and_the_bot_stays_quiet(
    world: World, mcp: McpHandle
) -> None:
    compiled = compiled_support()
    await say(world, mcp, compiled, "Quero abrir um chamado")  # a flow is open
    await say(world, mcp, compiled, "Quero falar com um atendente")
    assert await owner(world) == "HANDOFF_PENDING"
    assert (await replies(world))[-1].startswith("Claro, vou chamar um atendente")
    state = await world.db.pool.fetchval("SELECT state_json FROM conversation_states")
    assert state["flows"] == []  # the ticket flow ended

    llm = FakeLLM([])
    before = await world.count("outbox_messages")
    await say(world, mcp, compiled, "alô? alguém?", llm)
    assert await world.count("outbox_messages") == before and llm.calls == 0  # silent (INV-019)
    assert mcp.calls == []  # and nothing was called anywhere


async def test_a_person_can_return_the_conversation_and_the_bot_answers_again(
    world: World, mcp: McpHandle
) -> None:
    compiled = compiled_support()
    await say(world, mcp, compiled, "quero falar com uma pessoa")
    ops = OwnershipService(world.leases, world.uows, owner="ops")
    await ops.assign_human(KEY)
    await ops.return_to_bot(KEY)
    llm = FakeLLM([text_response("Estou de volta!")])
    await say(world, mcp, compiled, "oi", llm)
    assert (await replies(world))[-1] == "Estou de volta!" and await owner(world) == "BOT"


async def test_an_agent_with_nobody_behind_it_answers_honestly_and_keeps_the_conversation(
    world: World, mcp: McpHandle
) -> None:
    raw = manifest()
    raw["human_request"] = {
        **raw["human_request"],
        "available": False,
        "reply": "Aqui não consigo chamar um atendente.",
    }
    compiled = compile_manifest(raw)
    await say(world, mcp, compiled, "Quero abrir um chamado")
    await say(world, mcp, compiled, "quero falar com uma pessoa")
    assert (await replies(world))[-1] == "Aqui não consigo chamar um atendente."
    assert await owner(world) == "BOT"  # it did not hand over, because nobody would take it
    state = await world.db.pool.fetchval("SELECT state_json FROM conversation_states")
    assert len(state["flows"]) == 1  # the ticket is still being collected
    out = await say(world, mcp, compiled, "Não consigo entrar no sistema")
    assert out.status == "done" and "prioridade" in (await replies(world))[-1]
