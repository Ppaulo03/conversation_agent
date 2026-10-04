"""Release management (Phase 10): which published version new conversations get, canary,
rollback, an eval gate, an append-only history, and the coordinator following the release."""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any

import asyncpg
import pytest

from conftest import ApiHandle
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.manifest.yaml_loader import load_manifest_file
from conversation_agent.adapters.postgres.audit import PostgresAuditLog
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.releases import (
    PostgresReleaseResolver,
    ReleaseManager,
    ReleasePolicy,
)
from conversation_agent.adapters.registry.memory import InMemoryAgentRegistry
from conversation_agent.core.compiler import CompiledAgent, compile_manifest
from conversation_agent.core.releases import (
    ReleaseError,
    ReleaseEvidence,
    ReleaseState,
    bucket,
    desired_version,
    pick_previous,
    target_version,
    validate_evidence,
)
from postgres.world import KEY, World, event
from support.builders import IDENTITY
from vertical_slice.wiring import MANIFEST_PATH

TENANT = IDENTITY.tenant_id
AGENT_ID = "scheduling-demo"


def compiled(version: str) -> CompiledAgent:
    raw = copy.deepcopy(load_manifest_file(MANIFEST_PATH))
    raw["version"] = version
    raw["persona"] += f"\n\nPERSONA {version}."
    return compile_manifest(raw)


def evidence(agent: CompiledAgent, *, passed: int = 3, total: int = 3) -> ReleaseEvidence:
    return ReleaseEvidence(
        agent_digest=agent.digest, suite="smoke", suite_digest="f" * 64, passed=passed, total=total
    )


# --- the rules (pure) ---


def test_a_conversation_always_falls_on_the_same_side_and_widening_only_adds() -> None:
    conversations = [f"conv-{i}" for i in range(2000)]
    assert bucket("t", "a", "conv-1") == bucket("t", "a", "conv-1")
    at = {p: {c for c in conversations if bucket("t", "a", c) < p} for p in (10, 30, 100)}
    assert at[10] <= at[30] <= at[100] and len(at[100]) == 2000  # monotone: nobody is removed
    assert 0.07 < len(at[10]) / 2000 < 0.13  # and roughly the percentage asked for


def test_the_version_a_conversation_runs_on() -> None:
    canary = ReleaseState("1.0.0", "2.0.0", 30, frozenset({"0.9.0"}))
    assert desired_version(canary, 29) == "2.0.0" and desired_version(canary, 30) == "1.0.0"
    assert target_version(canary, None, True, 5) == "2.0.0"  # new: by bucket
    assert target_version(canary, "1.0.0", False, 5) == "1.0.0"  # in progress: never migrated
    assert target_version(canary, "1.0.0", True, 5) == "2.0.0"  # idle: the canary reaches it
    assert target_version(canary, "2.0.0", True, 99) == "2.0.0"  # never backwards when not pulled
    assert target_version(canary, "0.9.0", True, 99) == "1.0.0"  # a withdrawn one is left behind
    assert target_version(canary, "0.9.0", False, 99) == "0.9.0"  # ...but not while work is open


def test_evidence_must_be_for_this_version_and_passing() -> None:
    good = ReleaseEvidence(
        agent_digest="a" * 64, suite="s", suite_digest="b" * 64, passed=2, total=2
    )
    validate_evidence(good, agent_digest="a" * 64, required=True)
    for bad, digest in (
        (good, "c" * 64),  # somebody else's report
        (good.model_copy(update={"passed": 1}), "a" * 64),  # failing
        (good.model_copy(update={"passed": 0, "total": 0}), "a" * 64),  # nothing was run
        (None, "a" * 64),  # missing
    ):
        with pytest.raises(ReleaseError):
            validate_evidence(bad, agent_digest=digest, required=True)
    validate_evidence(None, agent_digest="a" * 64, required=False)


def test_the_previous_stable_skips_withdrawn_versions() -> None:
    assert pick_previous(["1.0.0", "1.1.0", "2.0.0"], "2.0.0", {"2.0.0", "1.1.0"}) == "1.0.0"
    assert pick_previous(["2.0.0"], "2.0.0", {"2.0.0"}) is None


# --- decisions on PostgreSQL ---


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


class Setup:
    def __init__(self, db: PostgresDatabase, **policy: Any) -> None:
        self.db = db
        self.registry = InMemoryAgentRegistry()
        self.manager = ReleaseManager(db, self.registry, ReleasePolicy(**policy))
        self.audit = PostgresAuditLog(db)
        self.agents = {v: compiled(v) for v in ("1.0.0", "1.1.0", "2.0.0")}

    async def publish(self, *versions: str) -> None:
        for version in versions:
            await self.registry.publish(TENANT, self.agents[version])

    async def promote(self, version: str, **kw: Any) -> ReleaseState:
        return await self.manager.promote(
            TENANT, AGENT_ID, version, actor="ops", reason="ship it",
            evidence=evidence(self.agents[version]), **kw,
        )  # fmt: skip

    async def canary(self, version: str, percent: int) -> ReleaseState:
        return await self.manager.start_canary(
            TENANT, AGENT_ID, version, percent, actor="ops", reason="try it",
            evidence=evidence(self.agents[version]),
        )  # fmt: skip


@pytest.fixture
async def rel(db: PostgresDatabase) -> Setup:
    setup = Setup(db)
    await setup.publish("1.0.0", "1.1.0", "2.0.0")
    return setup


async def test_a_version_must_be_published_and_evidenced_before_it_is_released(
    rel: Setup,
) -> None:
    manager = rel.manager
    with pytest.raises(ReleaseError, match="not published"):
        await manager.promote(TENANT, AGENT_ID, "9.9.9", actor="ops", reason="x")
    with pytest.raises(ReleaseError, match="needs eval evidence"):
        await manager.promote(TENANT, AGENT_ID, "1.0.0", actor="ops", reason="x")
    other = evidence(rel.agents["1.1.0"])  # a report about ANOTHER version
    with pytest.raises(ReleaseError, match="different agent"):
        await manager.promote(TENANT, AGENT_ID, "1.0.0", actor="ops", reason="x", evidence=other)
    failing = evidence(rel.agents["1.0.0"], passed=2, total=3)
    with pytest.raises(ReleaseError, match="did not pass"):
        await manager.promote(TENANT, AGENT_ID, "1.0.0", actor="ops", reason="x", evidence=failing)
    assert await manager.state(TENANT, AGENT_ID) is None  # nothing was released


async def test_a_deployment_that_chooses_no_gate_can_release_without_evidence(
    db: PostgresDatabase,
) -> None:
    ungated = Setup(db, require_evidence=False)
    await ungated.publish("1.0.0")
    state = await ungated.manager.promote(TENANT, AGENT_ID, "1.0.0", actor="ops", reason="hotfix")
    assert state.stable == "1.0.0"


async def test_promoting_records_state_history_and_audit_together(rel: Setup) -> None:
    await rel.promote("1.0.0")
    await rel.promote("1.1.0")
    state = await rel.manager.state(TENANT, AGENT_ID)
    assert state == ReleaseState("1.1.0", None, 0, frozenset())
    newest, oldest = await rel.manager.history(TENANT, AGENT_ID)
    assert (newest.event, newest.version, newest.actor, newest.reason) == (
        "promote", "1.1.0", "ops", "ship it"
    )  # fmt: skip
    assert newest.detail["previous"] == "1.0.0" and newest.detail["passed"] == 3
    assert oldest.version == "1.0.0"
    trail = await rel.audit.list(TENANT, action="release.promote")
    assert [r.subject_id for r in trail] == [f"{AGENT_ID}@1.1.0", f"{AGENT_ID}@1.0.0"]
    assert trail[0].details["suite"] == "smoke"


async def test_the_history_is_append_only_in_the_database_itself(rel: Setup) -> None:
    await rel.promote("1.0.0")
    with pytest.raises(asyncpg.PostgresError, match="append-only"):
        await rel.db.pool.execute("UPDATE agent_release_history SET reason = 'edited'")
    with pytest.raises(asyncpg.PostgresError, match="append-only"):
        await rel.db.pool.execute("DELETE FROM agent_release_history")


async def test_a_canary_needs_a_stable_release_a_newer_version_and_a_sane_percentage(
    rel: Setup,
) -> None:
    with pytest.raises(ReleaseError, match="no stable release yet"):
        await rel.canary("1.1.0", 10)
    await rel.promote("1.1.0")
    with pytest.raises(ReleaseError, match="newer than the stable"):
        await rel.canary("1.0.0", 10)
    for percent in (0, 101):
        with pytest.raises(ReleaseError, match="between 1 and 100"):
            await rel.canary("2.0.0", percent)
    state = await rel.canary("2.0.0", 20)
    assert (state.stable, state.candidate, state.candidate_percent) == ("1.1.0", "2.0.0", 20)


async def test_the_canary_is_widened_paused_and_graduated(rel: Setup) -> None:
    await rel.promote("1.0.0")
    await rel.canary("2.0.0", 10)
    resolver = PostgresReleaseResolver(rel.db)
    conversations = [f"c{i}" for i in range(300)]

    async def on_candidate() -> set[str]:
        return {
            c
            for c in conversations
            if await resolver.target(TENANT, AGENT_ID, c, None, True) == "2.0.0"
        }

    ten = await on_candidate()
    await rel.manager.set_canary_percent(TENANT, AGENT_ID, 40, actor="ops", reason="widen")
    forty = await on_candidate()
    assert ten <= forty and len(forty) > len(ten) > 0  # widening only adds conversations
    await rel.manager.set_canary_percent(TENANT, AGENT_ID, 0, actor="ops", reason="pause")
    assert await on_candidate() == set()
    with pytest.raises(ReleaseError, match="between 0 and 100"):
        await rel.manager.set_canary_percent(TENANT, AGENT_ID, 101, actor="ops", reason="x")

    state = await rel.promote("2.0.0")  # the canary graduates
    assert (state.stable, state.candidate, state.candidate_percent) == ("2.0.0", None, 0)
    assert await on_candidate() == set(conversations)


async def test_rolling_back_a_canary_withdraws_it_for_good(rel: Setup) -> None:
    await rel.promote("1.0.0")
    await rel.canary("2.0.0", 50)
    state = await rel.manager.rollback(TENANT, AGENT_ID, actor="oncall", reason="errors up")
    assert (state.stable, state.candidate, state.withdrawn) == ("1.0.0", None, {"2.0.0"})
    with pytest.raises(ReleaseError, match="withdrawn"):
        await rel.promote("2.0.0")
    with pytest.raises(ReleaseError, match="withdrawn"):
        await rel.canary("2.0.0", 5)
    assert (await rel.manager.history(TENANT, AGENT_ID))[0].event == "abort_canary"


async def test_rolling_back_the_stable_restores_the_previous_one(rel: Setup) -> None:
    await rel.promote("1.0.0")
    await rel.promote("1.1.0")
    await rel.promote("2.0.0")
    state = await rel.manager.rollback(TENANT, AGENT_ID, actor="oncall", reason="bad release")
    assert state.stable == "1.1.0" and state.withdrawn == {"2.0.0"}
    state = await rel.manager.rollback(TENANT, AGENT_ID, actor="oncall", reason="still bad")
    assert state.stable == "1.0.0" and state.withdrawn == {"2.0.0", "1.1.0"}  # skips withdrawn
    with pytest.raises(ReleaseError, match="no earlier stable"):
        await rel.manager.rollback(TENANT, AGENT_ID, actor="oncall", reason="nothing left")
    with pytest.raises(ReleaseError, match="nothing released"):
        await rel.manager.rollback("other-tenant", AGENT_ID, actor="x", reason="x")
    events = [e.event for e in await rel.manager.history(TENANT, AGENT_ID)]
    assert events[:2] == ["rollback", "rollback"]
    trail = await rel.audit.list(TENANT, action="release.rollback")
    assert trail[0].details["withdrawn"] == "1.1.0"


async def test_release_state_is_per_tenant(rel: Setup) -> None:
    await rel.promote("1.0.0")
    assert await rel.manager.state("another-tenant", AGENT_ID) is None
    resolver = PostgresReleaseResolver(rel.db)
    assert await resolver.target("another-tenant", AGENT_ID, "c", None, True) is None


# --- the coordinator follows the release ---


async def turn(world: World, api: ApiHandle, rel: Setup, text: str, n: int, llm: Any = None) -> Any:
    await world.inbox.insert_if_absent(
        event(f"r{n}", text, clock=world.clock, provider_occurred_at=world.clock.now())
    )
    coordinator = world.versioned_coordinator(
        "w",
        llm or FakeLLM([text_response("Olá!")]),
        rel.registry,
        AGENT_ID,
        providers={"http": world.http_provider(api.base_url)},
        releases=PostgresReleaseResolver(world.db),
    )
    return await coordinator.process_conversation(KEY)


async def pinned(world: World) -> str | None:
    return await world.db.pool.fetchval("SELECT agent_version FROM conversation_states")


async def test_new_conversations_get_the_stable_release_not_the_latest_published(
    world: World, api: ApiHandle
) -> None:
    rel = Setup(world.db)
    await rel.publish("1.0.0", "2.0.0")  # 2.0.0 is published but NOT released
    await rel.promote("1.0.0")
    await turn(world, api, rel, "oi", 1)
    assert await pinned(world) == "1.0.0"


async def test_an_idle_conversation_follows_a_promotion_and_a_rollback(
    world: World, api: ApiHandle
) -> None:
    rel = Setup(world.db)
    await rel.publish("1.0.0", "2.0.0")
    await rel.promote("1.0.0")
    await turn(world, api, rel, "oi", 1)
    await rel.promote("2.0.0")
    await turn(world, api, rel, "oi de novo", 2)
    assert await pinned(world) == "2.0.0"  # idle: it moved forward

    await rel.manager.rollback(TENANT, AGENT_ID, actor="oncall", reason="bad release")
    llm = FakeLLM([text_response("Olá!")])
    await turn(world, api, rel, "alguém aí?", 3, llm)
    assert await pinned(world) == "1.0.0"  # the withdrawn version is left behind
    assert (
        "PERSONA 1.0.0." in llm.requests[0].system
        and "PERSONA 2.0.0." not in llm.requests[0].system
    )


async def test_a_conversation_with_work_in_progress_is_not_migrated_by_a_rollback(
    world: World, api: ApiHandle
) -> None:
    rel = Setup(world.db)
    await rel.publish("1.0.0", "2.0.0")
    await rel.promote("1.0.0")
    await rel.promote("2.0.0")
    await turn(world, api, rel, "Quero marcar um corte amanhã às 10h", 1, FakeLLM([]))
    assert await pinned(world) == "2.0.0"  # a flow is now open on 2.0.0
    await rel.manager.rollback(TENANT, AGENT_ID, actor="oncall", reason="bad release")
    world.clock.set(world.clock.now() + timedelta(seconds=30))
    await turn(world, api, rel, "pode ser de manhã", 2, FakeLLM([text_response("ok")]))
    assert await pinned(world) == "2.0.0"  # finishes where it started: no migration under a flow


async def test_without_release_state_the_latest_published_version_still_applies(
    world: World, api: ApiHandle
) -> None:
    rel = Setup(world.db)
    await rel.publish("1.0.0", "2.0.0")
    await turn(world, api, rel, "oi", 1)
    assert await pinned(world) == "2.0.0"  # unchanged behaviour for agents with no release
