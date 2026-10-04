"""Hot-path metrics (Phase 11): what the coordinator reports about every pass over a turn, and how
it is exposed."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from conftest import ApiHandle
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.observability.prometheus import PREFIX, available_metrics, render
from conversation_agent.adapters.observability.runtime_metrics import InMemoryRuntimeMetrics
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.health import HealthSnapshot
from conversation_agent.core.errors import LLMProviderError
from conversation_agent.engine.ownership import OwnershipService
from postgres.test_handoff import handoff_agent
from postgres.test_protected_actions import coord
from postgres.world import KEY, World, event


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


def only(rt: InMemoryRuntimeMetrics) -> dict[str, int]:
    return {outcome: n for (_, _, outcome), n in rt.turns.items()}


async def test_a_completed_turn_reports_its_outcome_agent_calls_and_timings(world: World) -> None:
    rt = InMemoryRuntimeMetrics()
    await world.inbox.insert_if_absent(event("e-1", "oi", clock=world.clock))
    await world.db.pool.execute(
        "UPDATE inbox_events SET received_at = $1", world.clock.now() - timedelta(seconds=12)
    )
    await world.coordinator("c", FakeLLM([text_response("Olá!")]), metrics=rt).run_once()
    assert only(rt) == {"completed": 1}
    ((agent_id, version, _),) = rt.turns
    assert agent_id and version != "unknown"  # labelled with the agent that really ran
    who = (agent_id, version)
    assert rt.processing[who].count == 1 and rt.llm_calls[who] == 1 and rt.proposals[who] == 0
    assert rt.queue_wait[who].total == pytest.approx(12.0)  # it waited 12 s before pickup
    assert rt.handoffs == {}


async def test_a_proposal_is_counted_and_costs_no_model_call(world: World, api: ApiHandle) -> None:
    rt = InMemoryRuntimeMetrics()
    await world.inbox.insert_if_absent(
        event("e-1", "Quero marcar um corte amanhã às 10h", clock=world.clock)
    )
    await coord(world, api, "w", FakeLLM([]), flows=True, metrics=rt).process_conversation(KEY)
    ((agent_id, version, outcome),) = rt.turns
    assert outcome == "completed"
    assert rt.proposals[(agent_id, version)] == 1 and rt.llm_calls[(agent_id, version)] == 0


async def test_a_conversation_a_person_owns_is_counted_as_silent(world: World) -> None:
    rt = InMemoryRuntimeMetrics()
    await world.inbox.insert_if_absent(event("e-1", "oi", clock=world.clock))
    await world.coordinator("c", FakeLLM([text_response("Olá!")])).run_once()
    await OwnershipService(world.leases, world.uows, owner="ops").assign_human(KEY)
    await world.inbox.insert_if_absent(event("e-2", "alô?", clock=world.clock))
    await world.coordinator("c2", FakeLLM([]), metrics=rt).run_once()
    assert only(rt) == {"silent": 1}


async def test_provider_failures_are_retries_then_one_permanent_failure(world: World) -> None:
    rt = InMemoryRuntimeMetrics()
    await world.inbox.insert_if_absent(event("e-1", "oi", clock=world.clock))
    llm = FakeLLM([LLMProviderError("down")] * 4)
    coordinator = world.coordinator("w", llm, metrics=rt, max_turn_attempts=2)
    await coordinator.process_conversation(KEY)  # first failure: the turn stays open
    await coordinator.process_conversation(KEY)  # second: given up
    assert only(rt) == {"retry": 1, "failed": 1}


async def test_a_handoff_is_counted(world: World, api: ApiHandle) -> None:
    rt = InMemoryRuntimeMetrics()
    api.state.fault = {"status": 503}  # the schedule cannot be consulted: the flow gives up
    await world.inbox.insert_if_absent(
        event("e1", "Quero marcar um corte amanhã às 10h", clock=world.clock)
    )
    await world.coordinator(
        "c",
        FakeLLM([]),
        flows=True,
        agent=handoff_agent(),
        providers={"http": world.http_provider(api.base_url)},
        metrics=rt,
    ).run_once()
    assert sum(rt.handoffs.values()) == 1 and only(rt) == {"completed": 1}


async def test_a_failing_metrics_backend_never_changes_what_a_turn_does(world: World) -> None:
    class Broken:
        def record_turn(self, **kw: Any) -> None:
            raise RuntimeError("down")

        def record_handoff(self, **kw: Any) -> None:
            raise RuntimeError("down")

    await world.inbox.insert_if_absent(event("e-1", "oi", clock=world.clock))
    run = await world.coordinator(
        "c",
        FakeLLM([text_response("Olá!")]),
        metrics=Broken(),  # type: ignore[arg-type]
    ).run_once()
    assert [r.status for r in run] == ["done"]
    assert await world.count("outbox_messages") == 1


def test_the_runtime_metrics_are_exposed_with_valid_histograms() -> None:
    rt = InMemoryRuntimeMetrics()
    for processing, queue in ((0.3, 1.0), (5.0, 70.0), (200.0, None)):
        rt.record_turn(agent_id="a", agent_version="1.0.0", outcome="completed",
                       processing_seconds=processing, queue_seconds=queue, llm_calls=2, proposed=1)  # fmt: skip
    rt.record_turn(agent_id="a", agent_version="1.0.0", outcome="failed", processing_seconds=1,
                   queue_seconds=None, llm_calls=0, proposed=0)  # fmt: skip
    rt.record_handoff(agent_id="a", agent_version="1.0.0")
    text = render(HealthSnapshot(), None, None, rt)
    labels = 'agent_id="a",agent_version="1.0.0"'
    assert f'{PREFIX}turns_total{{{labels},outcome="completed"}} 3' in text
    assert f'{PREFIX}turns_total{{{labels},outcome="failed"}} 1' in text
    assert f"{PREFIX}turn_llm_calls_total{{{labels}}} 6" in text
    assert f"{PREFIX}handoffs_total{{{labels}}} 1" in text
    base = f"{PREFIX}turn_processing_seconds_bucket{{{labels},"
    counts = [int(line.rsplit(" ", 1)[1]) for line in text.splitlines() if line.startswith(base)]
    assert counts == sorted(counts) and counts[-1] == 4  # cumulative, +Inf = every observation
    assert f"{PREFIX}turn_queue_wait_seconds_count{{{labels}}} 2" in text  # unknown waits skipped
    names = {
        line.split("{")[0].split(" ")[0] for line in text.splitlines() if not line.startswith("#")
    }
    assert names <= available_metrics()
