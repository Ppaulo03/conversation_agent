"""Phase 9 on the durable runtime: the support agent (second domain) runs the whole protected
protocol over an MCP server, and shares one registry and one runtime with the scheduling agent."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from conftest import McpHandle
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.registry.postgres import PostgresAgentRegistry
from conversation_agent.core.models.runtime import InvocationStatus
from postgres.world import KEY, World, event
from support.builders import IDENTITY
from support.support_domain import compiled_support, pipeline_for
from vertical_slice.wiring import load_compiled_agent

TENANT = IDENTITY.tenant_id


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


class Support:
    def __init__(self, world: World, mcp: McpHandle) -> None:
        self.world, self.mcp = world, mcp
        self.compiled = compiled_support()
        self.pipeline = pipeline_for(self.compiled, mcp)

    async def say(self, text: str, *, after_s: float = 0, llm: Any = None) -> Any:
        world = self.world
        world.clock.set(world.clock.now() + timedelta(seconds=after_s))
        await world.inbox.insert_if_absent(
            event(
                f"e-{world.clock.now().isoformat()}-{text[:8]}",
                text,
                clock=world.clock,
                provider_occurred_at=world.clock.now(),
            )
        )
        coordinator = world.coordinator(
            "w", llm or FakeLLM([text_response("Pronto.")]), pipeline=self.pipeline, flows=True
        )
        return await coordinator.process_conversation(KEY)

    async def propose_ticket(self) -> None:
        await self.say("Quero abrir um chamado")
        await self.say("Não consigo entrar no sistema")
        await self.say("é urgente")
        assert await self.world.outbox_worker("sender").run_once() >= 1  # the prompt is delivered

    def creates(self) -> list[tuple[str, dict[str, object]]]:
        return [c for c in self.mcp.calls if c[0] == "create_ticket"]


@pytest.fixture
def support(world: World, mcp: McpHandle) -> Support:
    return Support(world, mcp)


async def test_a_confirmed_ticket_is_created_once_with_the_stable_idempotency_key(
    support: Support,
) -> None:
    await support.propose_ticket()
    assert support.creates() == [] and support.mcp.state.tickets == {}  # only proposed so far
    run = await support.say("sim", after_s=60)
    assert run.status == "done"
    (created,) = support.creates()
    assert created[1] == {"subject": "Não consigo entrar no sistema", "priority": "high"}
    (key,) = support.mcp.state.tickets  # the server keyed the ticket by the Idempotency-Key
    status = await support.world.db.pool.fetchval(
        "SELECT status FROM tool_invocations WHERE invocation_id = $1", key
    )
    assert status == InvocationStatus.SUCCEEDED.value


async def test_a_rejected_ticket_is_never_created(support: Support) -> None:
    await support.propose_ticket()
    await support.say("não", after_s=60)
    assert support.creates() == [] and support.mcp.state.tickets == {}


async def test_a_lost_answer_is_retried_with_the_same_key_and_never_duplicates(
    support: Support,
) -> None:
    await support.propose_ticket()
    support.mcp.state.call_fault = {"status_after_effect": 503}  # it was created, answer lost
    await support.say("sim", after_s=60)
    support.mcp.state.call_fault = None
    assert len(support.mcp.state.tickets) == 1  # it happened once...

    await support.world.reconciler("r", providers={}, pipeline=support.pipeline).run_once()
    assert len(support.mcp.state.tickets) == 1  # ...and the retry was a replay by key
    key = next(iter(support.mcp.state.tickets))
    status = await support.world.db.pool.fetchval(
        "SELECT status FROM tool_invocations WHERE invocation_id = $1", key
    )
    assert status in (InvocationStatus.RECONCILED.value, InvocationStatus.SUCCEEDED.value)


async def test_a_server_that_changed_its_tool_after_the_proposal_is_not_called(
    support: Support,
) -> None:
    await support.propose_ticket()
    for tool in support.mcp.state.tools:
        if tool["name"] == "create_ticket":  # the server now also demands an account id
            schema = tool["inputSchema"]
            tool["inputSchema"] = {
                **schema,
                "properties": {**schema["properties"], "account": {"type": "string"}},
                "required": [*schema["required"], "account"],
            }
    await support.say("sim", after_s=60)
    assert support.creates() == [] and support.mcp.state.tickets == {}  # nothing was sent
    texts = [
        r["text"]
        for r in await support.world.db.pool.fetch(
            "SELECT text FROM outbox_messages ORDER BY created_at"
        )
    ]
    assert any("chamar um atendente" in t for t in texts)  # the Flow's own fallback: a person
    ownership = await support.world.db.pool.fetchval("SELECT ownership FROM conversation_states")
    assert ownership == "HANDOFF_PENDING"


async def test_two_domains_share_one_registry_and_one_runtime(world: World) -> None:
    registry = PostgresAgentRegistry(world.db)
    scheduling, support = load_compiled_agent(), compiled_support()
    await registry.publish(TENANT, scheduling)
    await registry.publish(TENANT, support)
    for compiled in (scheduling, support):
        fresh = await PostgresAgentRegistry(world.db).get(TENANT, compiled.agent_id, "0.1.0")
        assert fresh is not None and fresh.digest == compiled.digest
    assert scheduling.agent_id != support.agent_id and scheduling.digest != support.digest
