"""Phase 8 on the durable runtime: the same Pack, installed by two agents, runs the whole protected
protocol (proposal -> confirmation -> prepared write -> execution -> reconciliation) over two
different external APIs. Only the agents' own bindings and mappings differ."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from conftest import AgendaHandle, ApiHandle
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.registry.postgres import PostgresAgentRegistry
from conversation_agent.core.compiler import compile_manifest
from conversation_agent.core.models.runtime import InvocationStatus
from postgres.world import KEY, World, event
from support.builders import IDENTITY
from support.pack_hosts import (
    HOSTS,
    HostSpec,
    bookings_of,
    compile_host,
    pipeline_for,
)

TENANT = IDENTITY.tenant_id


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


class Hosted:
    """One agent (compiled from its manifest + the Pack) over its own API."""

    def __init__(self, world: World, host: HostSpec, handle: Any) -> None:
        self.world, self.host, self.handle = world, host, handle
        self.compiled = compile_host(host.name)
        self.pipeline = pipeline_for(self.compiled, handle.base_url, host.connection)

    def coordinator(self, owner: str, llm: Any) -> Any:
        return self.world.coordinator(owner, llm, pipeline=self.pipeline, flows=True)

    async def say(self, text: str, *, after_s: float = 0, owner: str = "w") -> Any:
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
        llm = FakeLLM([text_response("Agendado!")])
        return await self.coordinator(owner, llm).process_conversation(KEY)

    async def propose(self) -> None:
        run = await self.say(f"Quero marcar {self.host.service_word} amanhã às 10h")
        assert run.status == "done"
        assert await self.world.outbox_worker("sender").run_once() >= 1  # the prompt is delivered

    def posts(self) -> list[dict[str, Any]]:
        return [
            r
            for r in self.handle.requests
            if r["method"] == "POST" and r["path"] == self.host.write_path
        ]


@pytest.fixture(params=HOSTS, ids=lambda h: h.name)
def hosted(
    request: pytest.FixtureRequest, world: World, api: ApiHandle, agenda: AgendaHandle
) -> Hosted:
    host: HostSpec = request.param
    return Hosted(world, host, api if host.fixture == "api" else agenda)


async def pending_status(world: World) -> list[str]:
    rows = await world.db.pool.fetch("SELECT status FROM pending_actions ORDER BY created_at")
    return [r["status"] for r in rows]


async def test_a_pack_proposal_waits_for_confirmation_and_writes_nothing(hosted: Hosted) -> None:
    await hosted.propose()
    assert await pending_status(hosted.world) == ["PENDING_CONFIRMATION"]
    assert hosted.posts() == [] and bookings_of(hosted.handle) == {}  # nothing executed yet
    prompt = await hosted.world.db.pool.fetchval(
        "SELECT text FROM outbox_messages ORDER BY created_at"
    )
    assert "Posso confirmar?" in prompt and "Agendar" in prompt


async def test_a_confirmed_booking_is_written_once_with_the_stable_idempotency_key(
    hosted: Hosted,
) -> None:
    await hosted.propose()
    run = await hosted.say("sim", after_s=60, owner="answerer")
    assert run.status == "done"
    (post,) = hosted.posts()  # one real write, to THIS host's API, on its own path
    key = post["headers"]["idempotency-key"]  # the invocation identity, never regenerated
    status = await hosted.world.db.pool.fetchval(
        "SELECT status FROM tool_invocations WHERE invocation_id = $1", key
    )
    assert status == InvocationStatus.SUCCEEDED.value
    assert len(bookings_of(hosted.handle)) == 1


async def test_a_lost_answer_is_reconciled_through_the_hosts_own_lookup_binding(
    hosted: Hosted,
) -> None:
    await hosted.propose()
    hosted.handle.state.fault = {"status_after_effect": 503}  # the booking exists, answer lost
    await hosted.say("sim", after_s=60, owner="answerer")
    hosted.handle.state.fault = None
    assert len(bookings_of(hosted.handle)) == 1  # it happened...

    (final,) = await hosted.world.reconciler(
        "r",
        providers={},  # the pipeline already carries this host's provider
        pipeline=hosted.pipeline,
    ).run_once()
    assert final.status is InvocationStatus.RECONCILED  # ...and was found, not repeated
    assert len(hosted.posts()) == 1 and len(bookings_of(hosted.handle)) == 1
    looked_up = [r["path"] for r in hosted.handle.requests if r["method"] == "GET"]
    assert any("por-chave" in p or "by-idempotency-key" in p for p in looked_up)


async def test_an_agent_that_installed_a_pack_publishes_and_reloads_without_it(
    world: World,
) -> None:
    registry = PostgresAgentRegistry(world.db)
    compiled = compile_host("studio")
    await registry.publish(TENANT, compiled)
    fresh = await PostgresAgentRegistry(world.db).get(TENANT, "studio-demo", "0.1.0")
    assert fresh is not None and fresh.digest == compiled.digest
    assert fresh.manifest_digest == compiled.manifest_digest
    assert fresh.manifest is not None and [p.name for p in fresh.manifest.pack_lock] == [
        "generic-scheduling"
    ]
    assert fresh.manifest.packs == ()  # the registry holds a self-contained agent
    assert compile_manifest(fresh.manifest).digest == compiled.digest  # no Pack needed to load it
