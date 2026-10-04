"""Phase 5 runtime: published agents are immutable, conversations are pinned to a version, and
an invocation is always reconciled with the version that prepared it."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import asyncpg
import pytest

from conftest import ApiHandle
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response, tool_call_response
from conversation_agent.adapters.manifest.yaml_loader import load_manifest_file
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.registry.memory import InMemoryAgentRegistry
from conversation_agent.adapters.registry.postgres import PostgresAgentRegistry
from conversation_agent.core.compiler import CompiledAgent, compile_agent, compile_manifest
from conversation_agent.core.errors import (
    IncompatibleUpgradeError,
    PublishError,
    RegistryIntegrityError,
    VersionConflictError,
    VersionRegressionError,
)
from conversation_agent.core.models.runtime import InvocationStatus
from postgres.world import KEY, AllowWrites, World, event
from vertical_slice.definitions import build_agent
from vertical_slice.wiring import MANIFEST_PATH

AGENT_ID = "scheduling-demo"
BOOKING = {
    "service_id": "haircut",
    "start_at": "2026-10-06T10:00:00-03:00",
    "duration_minutes": 30,
}


def manifest(version: str, **changes: Any) -> dict[str, Any]:
    raw = copy.deepcopy(load_manifest_file(MANIFEST_PATH))
    raw["version"] = version
    raw.update(changes)
    return raw


def compiled(version: str, **changes: Any) -> CompiledAgent:
    return compile_manifest(manifest(version, **changes))


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


# --- publishing rules (storage independent) ---


async def test_publishing_the_same_content_twice_is_a_noop() -> None:
    registry = InMemoryAgentRegistry()
    first = await registry.publish(compiled("1.0.0"))
    again = await registry.publish(compiled("1.0.0"))
    assert first.created and not again.created and again.digest == first.digest


async def test_a_published_version_is_immutable() -> None:
    registry = InMemoryAgentRegistry()
    await registry.publish(compiled("1.0.0"))
    with pytest.raises(VersionConflictError):
        await registry.publish(compiled("1.0.0", persona="Outra persona."))


async def test_versions_only_move_forward_and_order_numerically() -> None:
    registry = InMemoryAgentRegistry()
    for version in ("0.9.0", "0.10.0"):
        await registry.publish(compiled(version))
    assert await registry.versions(AGENT_ID) == ["0.9.0", "0.10.0"]  # not string order
    latest = await registry.latest(AGENT_ID)
    assert latest is not None and latest.version == "0.10.0"
    with pytest.raises(VersionRegressionError):
        await registry.publish(compiled("0.9.5"))


def without_capability(version: str) -> dict[str, Any]:
    raw = manifest(version)
    raw["capabilities"] = [
        c for c in raw["capabilities"] if c["name"] != "scheduling.lookup_booking"
    ]
    raw["bindings"] = [b for b in raw["bindings"] if b["capability"] != "scheduling.lookup_booking"]
    for tool in raw["tools"]:
        if tool["name"] == "erp_create_reservation":
            tool["recovery"] = {"strategy": "human_handoff"}
    return raw


async def test_a_breaking_change_needs_a_major_bump() -> None:
    registry = InMemoryAgentRegistry()
    await registry.publish(compiled("1.0.0"))
    with pytest.raises(IncompatibleUpgradeError, match="was removed"):
        await registry.publish(compile_manifest(without_capability("1.1.0")))
    published = await registry.publish(compile_manifest(without_capability("2.0.0")))
    assert published.created and any("was removed" in c for c in published.breaking_changes)


async def test_lowering_a_capability_risk_is_a_breaking_change() -> None:
    registry = InMemoryAgentRegistry()
    await registry.publish(compiled("1.0.0"))
    raw = manifest("1.1.0")
    create = next(c for c in raw["capabilities"] if c["name"] == "scheduling.create")
    create["risk"] = "write"
    for tool in raw["tools"]:
        if tool["name"] == "erp_create_reservation":
            tool["risk"] = "write"
    with pytest.raises(IncompatibleUpgradeError, match="lowered its risk"):
        await registry.publish(compile_manifest(raw))


# --- durable registry ---


async def test_the_durable_registry_roundtrips_and_checks_the_digest(world: World) -> None:
    registry = PostgresAgentRegistry(world.db)
    original = compiled("1.0.0")
    await registry.publish(original)
    fresh = await PostgresAgentRegistry(world.db).get(AGENT_ID, "1.0.0")  # another process
    assert fresh is not None and fresh.digest == original.digest
    assert fresh.agent.agent_id == AGENT_ID and len(fresh.agent.flows) == 1


async def test_published_rows_are_immutable_in_the_database_itself(world: World) -> None:
    registry = PostgresAgentRegistry(world.db)
    await registry.publish(compiled("1.0.0"))
    with pytest.raises(asyncpg.PostgresError, match="immutable"):
        await world.db.pool.execute("UPDATE published_agents SET digest = 'x'")
    with pytest.raises(asyncpg.PostgresError, match="immutable"):
        await world.db.pool.execute("DELETE FROM published_agents")


async def test_a_stored_agent_that_no_longer_matches_its_digest_is_refused(world: World) -> None:
    good = compiled("1.0.0")
    assert good.manifest is not None
    await world.db.pool.execute(
        "INSERT INTO published_agents (agent_id, version, digest, manifest_json, schema_version, "
        "compiler_version) VALUES ($1,$2,$3,$4,1,'1')",
        AGENT_ID,
        "1.0.0",
        "0" * 64,  # not what this manifest compiles to
        good.manifest.model_dump(mode="json", by_alias=True),
    )
    with pytest.raises(RegistryIntegrityError):
        await PostgresAgentRegistry(world.db).get(AGENT_ID, "1.0.0")


async def test_python_agents_cannot_be_published_durably(world: World) -> None:
    with pytest.raises(PublishError, match="manifest"):
        await PostgresAgentRegistry(world.db).publish(compile_agent(build_agent(flows=True)))


async def test_concurrent_publishers_cannot_both_win_the_same_version(world: World) -> None:
    a, b = PostgresAgentRegistry(world.db), PostgresAgentRegistry(world.db)
    results = await asyncio.gather(
        a.publish(compiled("1.0.0", persona="A")),
        b.publish(compiled("1.0.0", persona="B")),
        return_exceptions=True,
    )
    assert sum(isinstance(r, VersionConflictError) for r in results) == 1
    assert await world.count("published_agents") == 1


# --- pinning ---


async def say(world: World, api: ApiHandle, registry: Any, text: str, llm: Any, n: int) -> Any:
    await world.inbox.insert_if_absent(
        event(f"v{n}", text, clock=world.clock, provider_occurred_at=world.clock.now())
    )
    coordinator = world.versioned_coordinator(
        "w",
        llm,
        registry,
        AGENT_ID,
        providers={"http": world.http_provider(api.base_url)},
    )
    return await coordinator.process_conversation(KEY)


async def pinned_version(world: World) -> str | None:
    return await world.db.pool.fetchval("SELECT agent_version FROM conversation_states")


async def test_a_conversation_stays_on_its_version_while_something_is_in_progress(
    world: World, api: ApiHandle
) -> None:
    registry = InMemoryAgentRegistry()
    await registry.publish(compiled("0.1.0"))
    await say(world, api, registry, "Quero marcar um corte amanhã às 10h", FakeLLM([]), 1)
    assert await pinned_version(world) == "0.1.0"  # pinned on the first turn

    await registry.publish(compiled("0.2.0", persona="PERSONA NOVA."))  # a deploy mid-flow
    await world.outbox_worker("sender").run_once()  # the confirmation prompt reaches the channel
    world.clock.set(world.clock.now() + __import__("datetime").timedelta(seconds=30))
    llm = FakeLLM([text_response("Agendado!")])
    await say(world, api, registry, "sim", llm, 2)
    assert await pinned_version(world) == "0.1.0"  # a flow + pending action were in progress
    context = await world.db.pool.fetchval("SELECT context_json FROM tool_invocations")
    assert context["agent_version"] == "0.1.0"  # the write was prepared under the old version

    llm = FakeLLM([text_response("Olá!")])
    await say(world, api, registry, "oi", llm, 3)  # idle now: moves to the latest
    assert await pinned_version(world) == "0.2.0"
    assert "PERSONA NOVA." in llm.requests[0].system  # and really runs on it


async def test_a_missing_pinned_version_leaves_the_turn_open_and_runs_nothing(
    world: World, api: ApiHandle
) -> None:
    registry = InMemoryAgentRegistry()
    await registry.publish(compiled("0.1.0"))
    await say(world, api, registry, "Quero marcar um corte amanhã às 10h", FakeLLM([]), 1)
    await world.db.pool.execute("UPDATE conversation_states SET agent_version = '9.9.9'")
    llm = FakeLLM([])
    run = await say(world, api, registry, "outra coisa", llm, 2)
    assert run.status == "retry_later" and llm.calls == 0
    assert await world.count("turns", "status='PROCESSING'") == 1  # waits for an operator


# --- reconciliation uses the version that prepared the invocation ---


async def unknown_write_on_v1(world: World, api: ApiHandle, registry: Any) -> None:
    await registry.publish(compiled("0.1.0"))
    await world.inbox.insert_if_absent(event("w1", "quero terça 10h", clock=world.clock))
    api.state.fault = {"status_after_effect": 503}  # the booking exists, the answer is lost
    coordinator = world.versioned_coordinator(
        "w",
        FakeLLM([tool_call_response("scheduling__create", BOOKING)]),
        registry,
        AGENT_ID,
        providers={"http": world.http_provider(api.base_url)},
        policy=AllowWrites(frozenset({"scheduling.availability", "scheduling.create"})),
    )
    assert (await coordinator.process_conversation(KEY)).status == "waiting"
    api.state.fault = None


async def test_an_unknown_write_is_reconciled_with_the_version_that_prepared_it(
    world: World, api: ApiHandle
) -> None:
    registry = InMemoryAgentRegistry()
    await unknown_write_on_v1(world, api, registry)

    v2 = manifest("0.2.0")  # a deploy that moves the write endpoint
    next(t for t in v2["tools"] if t["name"] == "erp_create_reservation")["http"]["path"] = (
        "/v2/bookings"
    )
    await registry.publish(compile_manifest(v2))

    (resolved,) = await world.versioned_reconciler(
        "r", registry, AGENT_ID, providers={"http": world.http_provider(api.base_url)}
    ).run_once()
    assert resolved.status is InvocationStatus.RECONCILED  # v1's frozen contract still applies
    assert len([r for r in api.requests if r["method"] == "POST"]) == 1  # nothing re-sent


async def test_reconciliation_never_substitutes_another_version(
    world: World, api: ApiHandle
) -> None:
    registry = InMemoryAgentRegistry()
    await unknown_write_on_v1(world, api, registry)
    other = InMemoryAgentRegistry()  # a registry that does not have 0.1.0
    await other.publish(compiled("0.2.0"))
    (resolved,) = await world.versioned_reconciler(
        "r", other, AGENT_ID, providers={"http": world.http_provider(api.base_url)}
    ).run_once()
    assert resolved.status is InvocationStatus.HUMAN_HANDOFF  # a person decides, nothing guessed
    assert resolved.error is not None and resolved.error["code"] == "AGENT_VERSION_UNAVAILABLE"
    assert len([r for r in api.requests if r["method"] == "POST"]) == 1
