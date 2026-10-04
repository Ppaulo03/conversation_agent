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
from support.builders import IDENTITY
from vertical_slice.definitions import build_agent
from vertical_slice.wiring import MANIFEST_PATH

AGENT_ID = "scheduling-demo"
TENANT = IDENTITY.tenant_id
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
    first = await registry.publish(TENANT, compiled("1.0.0"))
    again = await registry.publish(TENANT, compiled("1.0.0"))
    assert first.created and not again.created and again.digest == first.digest


async def test_a_published_version_is_immutable() -> None:
    registry = InMemoryAgentRegistry()
    await registry.publish(TENANT, compiled("1.0.0"))
    with pytest.raises(VersionConflictError):
        await registry.publish(TENANT, compiled("1.0.0", persona="Outra persona."))


async def test_versions_only_move_forward_and_order_numerically() -> None:
    registry = InMemoryAgentRegistry()
    for version in ("0.9.0", "0.10.0"):
        await registry.publish(TENANT, compiled(version))
    assert await registry.versions(TENANT, AGENT_ID) == ["0.9.0", "0.10.0"]  # not string order
    latest = await registry.latest(TENANT, AGENT_ID)
    assert latest is not None and latest.version == "0.10.0"
    with pytest.raises(VersionRegressionError):
        await registry.publish(TENANT, compiled("0.9.5"))


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
    await registry.publish(TENANT, compiled("1.0.0"))
    with pytest.raises(IncompatibleUpgradeError, match="was removed"):
        await registry.publish(TENANT, compile_manifest(without_capability("1.1.0")))
    published = await registry.publish(TENANT, compile_manifest(without_capability("2.0.0")))
    assert published.created and any("was removed" in c for c in published.breaking_changes)


async def test_lowering_a_capability_risk_is_a_breaking_change() -> None:
    registry = InMemoryAgentRegistry()
    await registry.publish(TENANT, compiled("1.0.0"))
    raw = manifest("1.1.0")
    create = next(c for c in raw["capabilities"] if c["name"] == "scheduling.create")
    create["risk"] = "write"
    for tool in raw["tools"]:
        if tool["name"] == "erp_create_reservation":
            tool["risk"] = "write"
    with pytest.raises(IncompatibleUpgradeError, match="lowered its effective risk"):
        await registry.publish(TENANT, compile_manifest(raw))


# --- durable registry ---


async def test_the_durable_registry_roundtrips_and_checks_the_digest(world: World) -> None:
    registry = PostgresAgentRegistry(world.db)
    original = compiled("1.0.0")
    await registry.publish(TENANT, original)
    fresh = await PostgresAgentRegistry(world.db).get(TENANT, AGENT_ID, "1.0.0")  # another process
    assert fresh is not None and fresh.digest == original.digest
    assert fresh.agent.agent_id == AGENT_ID and len(fresh.agent.flows) == 1


async def test_published_rows_are_immutable_in_the_database_itself(world: World) -> None:
    registry = PostgresAgentRegistry(world.db)
    await registry.publish(TENANT, compiled("1.0.0"))
    with pytest.raises(asyncpg.PostgresError, match="immutable"):
        await world.db.pool.execute("UPDATE published_agents SET digest = 'x'")
    with pytest.raises(asyncpg.PostgresError, match="immutable"):
        await world.db.pool.execute("DELETE FROM published_agents")


async def test_a_stored_agent_that_no_longer_matches_its_digest_is_refused(world: World) -> None:
    good = compiled("1.0.0")
    assert good.manifest is not None
    await world.db.pool.execute(
        "INSERT INTO published_agents (tenant_id, agent_id, version, digest, manifest_digest, "
        "manifest_json, schema_version, compiler_version) VALUES ($1,$2,$3,$4,$5,$6,1,'1')",
        TENANT,
        AGENT_ID,
        "1.0.0",
        "0" * 64,  # not what this manifest compiles to
        good.manifest_digest,
        good.manifest.model_dump(mode="json", by_alias=True, exclude_unset=True),
    )
    with pytest.raises(RegistryIntegrityError):
        await PostgresAgentRegistry(world.db).get(TENANT, AGENT_ID, "1.0.0")


async def test_python_agents_cannot_be_published_durably(world: World) -> None:
    with pytest.raises(PublishError, match="manifest"):
        await PostgresAgentRegistry(world.db).publish(
            TENANT, compile_agent(build_agent(flows=True))
        )


async def test_concurrent_publishers_cannot_both_win_the_same_version(world: World) -> None:
    a, b = PostgresAgentRegistry(world.db), PostgresAgentRegistry(world.db)
    results = await asyncio.gather(
        a.publish(TENANT, compiled("1.0.0", persona="A")),
        b.publish(TENANT, compiled("1.0.0", persona="B")),
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
    await registry.publish(TENANT, compiled("0.1.0"))
    await say(world, api, registry, "Quero marcar um corte amanhã às 10h", FakeLLM([]), 1)
    assert await pinned_version(world) == "0.1.0"  # pinned on the first turn

    await registry.publish(TENANT, compiled("0.2.0", persona="PERSONA NOVA."))  # a deploy mid-flow
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
    await registry.publish(TENANT, compiled("0.1.0"))
    await say(world, api, registry, "Quero marcar um corte amanhã às 10h", FakeLLM([]), 1)
    await world.db.pool.execute("UPDATE conversation_states SET agent_version = '9.9.9'")
    llm = FakeLLM([])
    run = await say(world, api, registry, "outra coisa", llm, 2)
    assert run.status == "retry_later" and llm.calls == 0
    assert await world.count("turns", "status='PROCESSING'") == 1  # waits for an operator


# --- reconciliation uses the version that prepared the invocation ---


async def unknown_write_on_v1(world: World, api: ApiHandle, registry: Any) -> None:
    await registry.publish(TENANT, compiled("0.1.0"))
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
    await registry.publish(TENANT, compile_manifest(v2))

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
    await other.publish(TENANT, compiled("0.2.0"))
    (resolved,) = await world.versioned_reconciler(
        "r", other, AGENT_ID, providers={"http": world.http_provider(api.base_url)}
    ).run_once()
    assert resolved.status is InvocationStatus.HUMAN_HANDOFF  # a person decides, nothing guessed
    assert resolved.error is not None and resolved.error["code"] == "AGENT_VERSION_UNAVAILABLE"
    assert len([r for r in api.requests if r["method"] == "POST"]) == 1


# --- Phase 5.1 ---


def tiny(
    version: str, *, tool_risk: str = "read", tool_confirmation: bool = False
) -> CompiledAgent:
    """capability says `read`; the TOOL decides how protected it really is."""
    return compile_manifest(
        {
            "agent_id": "tiny",
            "version": version,
            "persona": "x",
            "capabilities": [
                {
                    "name": "demo.ping",
                    "description": "ping",
                    "input": {"x": {"type": "string"}},
                    "output": {"y": {"type": "string"}},
                }
            ],
            "tools": [
                {
                    "name": "ping_tool",
                    "description": "ping",
                    "risk": tool_risk,
                    "confirmation_required": tool_confirmation,
                    "provider": "http",
                    "connection": "c",
                    "http": {"method": "GET", "path": "/ping", "query": ["x"]},
                    "input": {"x": {"type": "string"}},
                    "output": {"y": {"type": "string"}},
                }
            ],
            "bindings": [
                {
                    "capability": "demo.ping",
                    "tool": "ping_tool",
                    "input_map": {"x": "$.x"},
                    "output_map": {"y": "$.y"},
                }
            ],
            "allowed_capabilities": ["demo.ping"],
        }
    )


async def test_a_minor_version_cannot_lower_the_effective_risk() -> None:
    registry = InMemoryAgentRegistry()
    await registry.publish(TENANT, tiny("1.0.0", tool_risk="irreversible"))
    with pytest.raises(IncompatibleUpgradeError, match="lowered its effective risk"):
        await registry.publish(
            TENANT, tiny("1.1.0", tool_risk="read")
        )  # capability label never changed
    published = await registry.publish(TENANT, tiny("2.0.0", tool_risk="read"))
    assert published.created and published.breaking_changes


async def test_a_minor_version_cannot_remove_effective_protection() -> None:
    registry = InMemoryAgentRegistry()
    await registry.publish(TENANT, tiny("1.0.0", tool_confirmation=True))
    with pytest.raises(IncompatibleUpgradeError, match="no longer requires"):
        await registry.publish(TENANT, tiny("1.1.0", tool_confirmation=False))


async def test_raising_protection_in_a_minor_version_is_fine() -> None:
    registry = InMemoryAgentRegistry()
    await registry.publish(TENANT, tiny("1.0.0"))
    assert (await registry.publish(TENANT, tiny("1.1.0", tool_confirmation=True))).created


async def test_a_conversation_is_pinned_to_the_agent_not_just_the_version(
    world: World, api: ApiHandle
) -> None:
    registry = InMemoryAgentRegistry()
    await registry.publish(TENANT, compiled("0.1.0"))
    await registry.publish(TENANT, compiled("0.1.0", agent_id="other-agent"))  # same version number
    await say(world, api, registry, "Quero marcar um corte amanhã às 10h", FakeLLM([]), 1)
    row = await world.db.pool.fetchrow("SELECT agent_id, agent_version FROM conversation_states")
    assert (row["agent_id"], row["agent_version"]) == (AGENT_ID, "0.1.0")

    # routing now sends this conversation to ANOTHER agent while a flow is open: fail closed
    await world.inbox.insert_if_absent(event("x1", "oi", clock=world.clock))
    llm = FakeLLM([])
    other = world.versioned_coordinator(
        "w2",
        llm,
        registry,
        "other-agent",
        providers={"http": world.http_provider(api.base_url)},
    )
    assert (await other.process_conversation(KEY)).status == "retry_later" and llm.calls == 0
    row = await world.db.pool.fetchrow("SELECT agent_id FROM conversation_states")
    assert row["agent_id"] == AGENT_ID  # nothing was swapped under the flow


async def test_an_idle_conversation_can_be_reassigned_to_another_agent(
    world: World, api: ApiHandle
) -> None:
    registry = InMemoryAgentRegistry()
    await registry.publish(TENANT, compiled("0.1.0"))
    await registry.publish(TENANT, compiled("0.1.0", agent_id="other-agent"))
    await say(world, api, registry, "oi", FakeLLM([text_response("Olá!")]), 1)  # idle: no flow
    await world.inbox.insert_if_absent(event("x2", "oi de novo", clock=world.clock))
    other = world.versioned_coordinator(
        "w2",
        FakeLLM([text_response("Olá, sou outro.")]),
        registry,
        "other-agent",
        providers={"http": world.http_provider(api.base_url)},
    )
    assert (await other.process_conversation(KEY)).status == "done"
    row = await world.db.pool.fetchrow("SELECT agent_id, agent_version FROM conversation_states")
    assert (row["agent_id"], row["agent_version"]) == ("other-agent", "0.1.0")


async def insert_row(world: World, **overrides: Any) -> None:
    good = compiled("1.0.0")
    assert good.manifest is not None
    values: dict[str, Any] = {
        "tenant_id": TENANT,
        "agent_id": AGENT_ID,
        "version": "1.0.0",
        "digest": good.digest,
        "manifest_digest": good.manifest_digest,
        "manifest_json": good.manifest.model_dump(mode="json", by_alias=True, exclude_unset=True),
        "schema_version": 1,
        "compiler_version": "1",
    } | overrides
    await world.db.pool.execute(
        "INSERT INTO published_agents (tenant_id, agent_id, version, digest, manifest_digest, "
        "manifest_json, schema_version, compiler_version) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
        *values.values(),
    )


async def test_the_row_identity_must_match_the_manifest_it_holds(world: World) -> None:
    other = compiled("2.0.0")  # same content, so the same digest, but another version label
    assert other.manifest is not None
    await insert_row(
        world,
        manifest_json=other.manifest.model_dump(mode="json", by_alias=True, exclude_unset=True),
    )
    with pytest.raises(RegistryIntegrityError, match="not what was published"):
        await PostgresAgentRegistry(world.db).get(TENANT, AGENT_ID, "1.0.0")


async def test_an_agent_from_an_unknown_compiler_version_is_refused_explicitly(
    world: World,
) -> None:
    from conversation_agent.core.errors import RegistryCompatibilityError

    await insert_row(world, compiler_version="2")
    with pytest.raises(RegistryCompatibilityError, match="compiler 2"):
        await PostgresAgentRegistry(world.db).get(TENANT, AGENT_ID, "1.0.0")


# --- Phase 5.2 ---


async def test_removing_a_capability_from_the_allowlist_is_a_breaking_change() -> None:
    registry = InMemoryAgentRegistry()

    def no_flows(version: str, allowed: list[str]) -> CompiledAgent:
        return compiled(version, flows=[], allowed_capabilities=allowed)

    both = ["scheduling.availability", "scheduling.create"]
    await registry.publish(TENANT, no_flows("1.0.0", both))
    with pytest.raises(IncompatibleUpgradeError, match="no longer allowed"):
        await registry.publish(TENANT, no_flows("1.1.0", ["scheduling.availability"]))
    assert (await registry.publish(TENANT, no_flows("2.0.0", ["scheduling.availability"]))).created


async def test_two_manifests_that_differ_only_in_metadata_are_not_the_same_publication() -> None:
    registry = InMemoryAgentRegistry()
    first = compiled("1.0.0")
    await registry.publish(TENANT, first)
    assert not (await registry.publish(TENANT, compiled("1.0.0"))).created  # identical: no-op
    other = compiled("1.0.0", min_framework="0.5.0")
    assert other.digest == first.digest and other.manifest_digest != first.manifest_digest
    with pytest.raises(VersionConflictError, match="different metadata"):
        await registry.publish(TENANT, other)


async def test_the_registry_is_tenant_scoped(world: World) -> None:
    for registry in (InMemoryAgentRegistry(), PostgresAgentRegistry(world.db)):
        await registry.publish("acme", compiled("1.0.0", persona="Persona da Acme."))
        await registry.publish("globex", compiled("1.0.0", persona="Persona da Globex."))  # same id
        acme = await registry.get("acme", AGENT_ID, "1.0.0")
        globex = await registry.get("globex", AGENT_ID, "1.0.0")
        assert acme is not None and globex is not None
        assert acme.agent.persona != globex.agent.persona  # no shadowing, no leakage
        assert await registry.get("initech", AGENT_ID, "1.0.0") is None
        assert await registry.versions("initech", AGENT_ID) == []
        assert await registry.latest("initech", AGENT_ID) is None
        await world.db.pool.execute("TRUNCATE published_agents")


class BrokenRegistry(InMemoryAgentRegistry):
    """Every lookup of a stored agent fails its integrity check."""

    async def get(self, tenant_id: str, agent_id: str, version: str) -> CompiledAgent | None:
        raise RegistryIntegrityError("digest mismatch")


async def test_a_corrupt_agent_stops_that_conversation_not_the_worker(
    world: World, api: ApiHandle
) -> None:
    good = InMemoryAgentRegistry()
    await good.publish(TENANT, compiled("0.1.0"))
    await say(world, api, good, "Quero marcar um corte amanhã às 10h", FakeLLM([]), 1)
    broken = BrokenRegistry()
    await broken.publish(TENANT, compiled("0.1.0"))
    llm = FakeLLM([])
    run = await say(world, api, broken, "outra coisa", llm, 2)
    assert run.status == "retry_later" and llm.calls == 0  # an alert, nothing executed, no crash
    assert await world.count("turns", "status='PROCESSING'") == 1


async def test_a_corrupt_definition_hands_that_invocation_to_a_human_and_the_batch_goes_on(
    world: World, api: ApiHandle
) -> None:
    registry = InMemoryAgentRegistry()
    await unknown_write_on_v1(world, api, registry)
    broken = BrokenRegistry()
    await broken.publish(TENANT, compiled("0.1.0"))
    (resolved,) = await world.versioned_reconciler(
        "r", broken, AGENT_ID, providers={"http": world.http_provider(api.base_url)}
    ).run_once()
    assert resolved.status is InvocationStatus.HUMAN_HANDOFF
    assert resolved.error is not None and resolved.error["code"] == "AGENT_VERSION_UNAVAILABLE"


async def test_a_stored_manifest_that_does_not_compile_is_an_integrity_error(world: World) -> None:
    # corruption / a bad restore: this must be the error the coordinator and reconciler contain
    await insert_row(world, manifest_json={"bad": 1})
    with pytest.raises(RegistryIntegrityError, match="does not compile"):
        await PostgresAgentRegistry(world.db).get(TENANT, AGENT_ID, "1.0.0")


async def test_changed_manifest_metadata_is_detected_even_when_the_semantics_match(
    world: World,
) -> None:
    other = compiled("1.0.0", min_framework="0.5.0")  # same behaviour, other artifact
    assert other.manifest is not None
    await insert_row(
        world,
        manifest_json=other.manifest.model_dump(mode="json", by_alias=True, exclude_unset=True),
    )  # digest and manifest_digest are those of the ORIGINAL publication
    with pytest.raises(RegistryIntegrityError, match="byte-for-byte"):
        await PostgresAgentRegistry(world.db).get(TENANT, AGENT_ID, "1.0.0")


async def test_a_corrupt_stored_manifest_stops_that_conversation_not_the_worker(
    world: World, api: ApiHandle
) -> None:
    registry = PostgresAgentRegistry(world.db)
    await registry.publish(TENANT, compiled("0.1.0"))
    await say(world, api, registry, "Quero marcar um corte amanhã às 10h", FakeLLM([]), 1)
    await world.db.pool.execute("ALTER TABLE published_agents DISABLE TRIGGER USER")
    await world.db.pool.execute("UPDATE published_agents SET manifest_json = '{\"bad\": 1}'")
    await world.db.pool.execute("ALTER TABLE published_agents ENABLE TRIGGER USER")
    fresh = PostgresAgentRegistry(world.db)  # nothing cached: it must read the broken row
    llm = FakeLLM([])
    run = await say(world, api, fresh, "outra coisa", llm, 2)
    assert run.status == "retry_later" and llm.calls == 0
