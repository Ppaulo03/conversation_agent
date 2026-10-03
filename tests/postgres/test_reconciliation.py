"""Reconciliation of UNKNOWN writes + durable scheduler, against PostgreSQL and the real API.

C05, C06, C11 and the recovery half of C04; INV-006 (UNKNOWN never blind-retries) and INV-021
(any re-send reuses the same idempotency key).
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from conftest import ApiHandle
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.faults import ChaosFaults, SimulatedCrash
from conversation_agent.adapters.llm.fake import FakeLLM, text_response, tool_call_response
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.scheduler import PostgresScheduler
from conversation_agent.adapters.tools.chaos import FaultInjectingToolProvider
from conversation_agent.core.definitions.tool import RecoverySpec
from conversation_agent.core.errors import ExecutionFencingError
from conversation_agent.core.models.runtime import (
    ExecutionClaim,
    InvocationStatus,
    ScheduledEvent,
    ToolInvocation,
)
from conversation_agent.core.models.tooling import ToolResult
from conversation_agent.engine.capability_pipeline import CapabilityPipeline
from conversation_agent.engine.tool_runner import ToolRunner
from conversation_agent.ports.llm import LLMRequest
from postgres.world import KEY, TTL, AllowWrites, World, event
from vertical_slice.definitions import ERP_CREATE_RESERVATION, build_agent

BOOKING_ARGS = {
    "service_id": "haircut",
    "start_at": "2026-10-06T10:00:00-03:00",
    "duration_minutes": 30,
}
ALLOW = frozenset({"scheduling.availability", "scheduling.create"})
EXEC_TTL = timedelta(seconds=60)


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


def posts(api: ApiHandle) -> list[dict[str, object]]:
    return [r for r in api.requests if r["method"] == "POST"]


def worker(world: World, api: ApiHandle, owner: str, llm: Any, *, provider: Any = None, **kw: Any):  # type: ignore[no-untyped-def]
    return world.coordinator(
        owner,
        llm,
        providers={"http": provider or world.http_provider(api.base_url)},
        policy=AllowWrites(ALLOW),
        **kw,
    )


def book() -> list[Any]:
    return [tool_call_response("scheduling__create", BOOKING_ARGS), text_response("Agendado!")]


def book_first() -> FakeLLM:
    return FakeLLM([tool_call_response("scheduling__create", BOOKING_ARGS)])


async def start(world: World) -> None:
    await world.inbox.insert_if_absent(event("e1", "quero terça 10h", clock=world.clock))


async def only_invocation(world: World) -> ToolInvocation:
    row = await world.db.pool.fetchval("SELECT invocation_id FROM tool_invocations")
    inv = await world.ledger.get(KEY.tenant_id, row)
    assert inv is not None
    return inv


def advance(world: World, seconds: float) -> None:
    world.clock.set(world.clock.now() + timedelta(seconds=seconds))


async def finish_turn(world: World, api: ApiHandle, seen: dict[str, Any] | None = None) -> None:
    """A later pass: the new owner replays the journal and applies the (now known) result."""

    def observe(request: LLMRequest) -> Any:
        parts = [p for m in request.messages for p in m.parts if hasattr(p, "tool_call_id")]
        if seen is not None:
            seen.update(json.loads(parts[-1].content))  # type: ignore[attr-defined]
        return text_response("Pronto!")

    run = await worker(world, api, "finisher", FakeLLM([observe])).process_conversation(KEY)
    assert run.status == "done"


# --- C05: write outcome ambiguous (the effect happened, the answer was lost) -----------------


async def test_C05_response_lost_write_is_unknown_then_status_lookup_adopts_the_real_result(
    world: World, api: ApiHandle
) -> None:
    await start(world)
    api.state.fault = {"status_after_effect": 503}  # booking IS created, caller sees a 503
    run = await worker(world, api, "w1", book_first()).process_conversation(KEY)
    assert run.status == "waiting"  # the turn waits: the outcome is not known
    inv = await only_invocation(world)
    assert inv.status is InvocationStatus.UNKNOWN and inv.result_application_status == "none"
    assert len(api.state.bookings) == 1 and len(posts(api)) == 1

    api.state.fault = None
    (resolved,) = await world.reconciler(
        "r1", providers={"http": world.http_provider(api.base_url)}
    ).run_once()
    assert resolved.status is InvocationStatus.RECONCILED
    assert resolved.result_application_status == "pending"
    assert len(posts(api)) == 1 and len(api.state.bookings) == 1  # found by lookup: no re-send

    seen: dict[str, Any] = {}
    await finish_turn(world, api, seen)
    assert seen["status"] == "success" and seen["data"]["booking_id"] == "bk_1"
    assert (await only_invocation(world)).result_application_status == "applied"
    assert await world.count("outbox_messages") == 1


async def test_C05_crash_after_external_send_is_recovered_without_duplicating(
    world: World, api: ApiHandle
) -> None:
    await start(world)
    faults = ChaosFaults("C05_after_external_send")
    flaky = FaultInjectingToolProvider(
        world.http_provider(api.base_url), faults, "C05_after_external_send"
    )
    with pytest.raises(SimulatedCrash):
        await worker(world, api, "w1", book_first(), provider=flaky).process_conversation(KEY)
    assert (await only_invocation(world)).status is InvocationStatus.EXECUTING
    assert len(api.state.bookings) == 1

    advance(world, 61)  # execution lease and conversation lease both expire
    (resolved,) = await world.reconciler(
        "r1", providers={"http": world.http_provider(api.base_url)}
    ).run_once()
    assert resolved.status is InvocationStatus.RECONCILED
    await finish_turn(world, api)
    assert len(posts(api)) == 1 and len(api.state.bookings) == 1


async def test_C06_after_external_success_before_ledger_commit_same_key_no_duplicate(
    world: World, api: ApiHandle
) -> None:
    await start(world)
    with pytest.raises(SimulatedCrash):
        await worker(
            world, api, "w1", book_first(), faults=ChaosFaults("C06_after_external_success")
        ).process_conversation(KEY)
    inv = await only_invocation(world)
    assert inv.status is InvocationStatus.EXECUTING  # success received, never recorded
    assert len(api.state.bookings) == 1

    advance(world, 61)
    (resolved,) = await world.reconciler(
        "r1", providers={"http": world.http_provider(api.base_url)}
    ).run_once()
    assert resolved.status is InvocationStatus.RECONCILED
    await finish_turn(world, api)
    assert len(posts(api)) == 1 and len(api.state.bookings) == 1


# --- C04: crash before the request -> lookup says "never happened" -> same-key re-send ---------


async def test_C04_never_sent_is_resent_with_the_same_idempotency_key_exactly_once(
    world: World, api: ApiHandle
) -> None:
    await start(world)
    with pytest.raises(SimulatedCrash):
        await worker(
            world, api, "w1", book_first(), faults=ChaosFaults("C04_before_external_request")
        ).process_conversation(KEY)
    assert posts(api) == [] and api.state.bookings == {}

    advance(world, 61)
    (resolved,) = await world.reconciler(
        "r1", providers={"http": world.http_provider(api.base_url)}
    ).run_once()
    assert resolved.status is InvocationStatus.RECONCILED
    (post,) = posts(api)
    assert post["headers"]["idempotency-key"] == resolved.invocation_id  # type: ignore[index]
    assert resolved.idempotency_key == resolved.invocation_id  # same identity, never regenerated
    await finish_turn(world, api)
    assert len(api.state.bookings) == 1


# --- C11: reconciler killed after claiming -------------------------------------------------------


async def test_C11_reconciler_killed_after_claim_is_superseded_by_epoch_no_blind_execution(
    world: World, api: ApiHandle
) -> None:
    await start(world)
    api.state.fault = {"status_after_effect": 503}
    await worker(world, api, "w1", book_first()).process_conversation(KEY)
    api.state.fault = None
    providers = {"http": world.http_provider(api.base_url)}

    with pytest.raises(SimulatedCrash):
        await world.reconciler(
            "r1", providers=providers, faults=ChaosFaults("C11_during_reconciliation")
        ).run_once()
    stuck = await only_invocation(world)
    assert stuck.status is InvocationStatus.RECONCILING
    dead_claim = ExecutionClaim(invocation_id=stuck.invocation_id, epoch=stuck.execution_epoch)

    advance(world, 61)  # r1's claim expires
    (resolved,) = await world.reconciler("r2", providers=providers).run_once()
    assert resolved.status is InvocationStatus.RECONCILED
    assert resolved.execution_epoch == dead_claim.epoch + 1  # a newer epoch fences r1 out

    with pytest.raises(ExecutionFencingError):  # r1 "wakes up" and tries to write its verdict
        await world.ledger.finalize_reconciliation(
            stuck.tenant_id,
            stuck.invocation_id,
            dead_claim,
            ToolResult(status="success", data={"booking_id": "bogus", "status": "x"}),
        )
    assert len(posts(api)) == 1 and len(api.state.bookings) == 1  # nothing ran blindly


# --- UNKNOWN never blind-retries; backoff survives via the durable scheduler -------------------


async def test_unknown_never_blind_retries_and_backs_off_on_a_durable_timer(
    world: World, api: ApiHandle
) -> None:  # INV-006
    await start(world)
    api.state.fault = {"status_after_effect": 503}
    await worker(world, api, "w1", book_first()).process_conversation(KEY)
    providers = {"http": world.http_provider(api.base_url)}

    api.state.fault = {"status": 503}  # now even the status lookup is unavailable
    assert [i.status for i in await world.reconciler("r1", providers=providers).run_once()] == [
        InvocationStatus.UNKNOWN  # still unknown: no verdict, therefore no retry
    ]
    assert len(posts(api)) == 1  # the lookup is a GET; the write was NOT re-sent
    assert await world.count("scheduled_events", "status='PENDING'") == 1
    assert await world.reconciler("r2", providers=providers).run_once() == []  # backing off

    api.state.fault = None
    advance(world, 31)  # backoff elapsed
    assert await world.scheduler_worker("sched").run_once() == 1  # the timer fires
    (resolved,) = await world.reconciler("r3", providers=providers).run_once()
    assert resolved.status is InvocationStatus.RECONCILED
    assert len(posts(api)) == 1 and len(api.state.bookings) == 1


async def test_exhausted_attempts_escalate_to_a_human_instead_of_looping(
    world: World, api: ApiHandle
) -> None:
    await start(world)
    api.state.fault = {"status_after_effect": 503}
    await worker(world, api, "w1", book_first()).process_conversation(KEY)
    providers = {"http": world.http_provider(api.base_url)}
    api.state.fault = {"status": 503}
    reconciler = world.reconciler("r", providers=providers, max_attempts=1)

    await reconciler.run_once()  # attempt 1: lookup down -> still unknown
    advance(world, 31)
    await world.scheduler_worker("s").run_once()
    (final,) = await reconciler.run_once()  # attempt 2 > max -> handoff
    assert final.status is InvocationStatus.HUMAN_HANDOFF
    assert final.result_application_status == "pending"
    assert final.result is not None and final.result["error"]["code"] == "RECONCILIATION_EXHAUSTED"
    assert len(posts(api)) == 1


async def test_no_recovery_contract_means_human_handoff_and_an_honest_reply(
    world: World, api: ApiHandle
) -> None:
    agent = build_agent()
    bare = ERP_CREATE_RESERVATION.model_copy(update={"recovery": None})
    no_contract = agent.model_copy(
        update={"tools": tuple(bare if t.name == bare.name else t for t in agent.tools)}
    )
    providers = {"http": world.http_provider(api.base_url)}
    pipeline = CapabilityPipeline(no_contract, AllowWrites(ALLOW), ToolRunner(providers))

    await start(world)
    api.state.fault = {"status_after_effect": 503}
    await worker(world, api, "w1", book_first(), pipeline=pipeline).process_conversation(KEY)
    api.state.fault = None
    (final,) = await world.reconciler("r1", providers=providers, pipeline=pipeline).run_once()
    assert final.status is InvocationStatus.HUMAN_HANDOFF
    assert len(posts(api)) == 1  # no automatic contract -> no automatic action

    seen: dict[str, Any] = {}
    await finish_turn(world, api, seen)
    assert seen["status"] == "unknown"  # the user is told the outcome is unconfirmed
    assert seen["error"]["code"] == "NO_AUTOMATIC_RECOVERY"


async def test_write_retry_requires_supported_idempotency_and_same_key(
    world: World, api: ApiHandle
) -> None:  # INV-021
    # the contract itself refuses an inconsistent declaration...
    with pytest.raises(ValidationError):
        ERP_CREATE_RESERVATION.model_copy(update={"idempotency_supported": False}).model_validate(
            {**ERP_CREATE_RESERVATION.model_dump(), "idempotency_supported": False,
             "recovery": RecoverySpec(strategy="retry_same_key")}
        )  # fmt: skip
    # ...and "not found" on a NON-idempotent tool escalates instead of re-sending
    agent = build_agent()
    risky = ERP_CREATE_RESERVATION.model_copy(update={"idempotency_supported": False})
    pipeline = CapabilityPipeline(
        agent.model_copy(
            update={"tools": tuple(risky if t.name == risky.name else t for t in agent.tools)}
        ),
        AllowWrites(ALLOW),
        ToolRunner({"http": world.http_provider(api.base_url)}),
    )
    await start(world)
    with pytest.raises(SimulatedCrash):  # crash before any request: the booking never happened
        await worker(
            world,
            api,
            "w1",
            book_first(),
            pipeline=pipeline,
            faults=ChaosFaults("C04_before_external_request"),
        ).process_conversation(KEY)
    advance(world, 61)
    (final,) = await world.reconciler(
        "r", providers={"http": world.http_provider(api.base_url)}, pipeline=pipeline
    ).run_once()
    assert final.status is InvocationStatus.HUMAN_HANDOFF
    assert posts(api) == []  # never re-sent without idempotency support


# --- durable scheduler ----------------------------------------------------------


def timer(world: World, key: str = "k1", *, due_in: float = 10) -> ScheduledEvent:
    return ScheduledEvent(
        tenant_id=KEY.tenant_id,
        scheduler_key=key,
        event_type="reconcile",
        due_at=world.clock.now() + timedelta(seconds=due_in),
        payload={"n": 1},
    )


async def test_timers_fire_only_when_due_upsert_and_cancel(world: World) -> None:
    await world.scheduler.schedule(timer(world, due_in=10))
    assert await world.scheduler.claim_due("w", 5, TTL) == []  # not due yet
    await world.scheduler.schedule(timer(world, due_in=20))  # same key: replaces, not duplicates
    assert await world.count("scheduled_events") == 1
    advance(world, 15)
    assert await world.scheduler.claim_due("w", 5, TTL) == []  # the replacement is due at +20
    advance(world, 6)
    (due,) = await world.scheduler.claim_due("w", 5, TTL)
    assert due.scheduler_key == "k1" and due.payload == {"n": 1}
    await world.scheduler.complete(due.tenant_id, due.scheduler_key, "w")
    assert await world.scheduler.claim_due("w", 5, TTL) == []

    await world.scheduler.schedule(timer(world, "k2", due_in=1))
    await world.scheduler.cancel(KEY.tenant_id, "k2")
    advance(world, 5)
    assert await world.scheduler.claim_due("w", 5, TTL) == []


async def test_timers_survive_a_restart_and_an_abandoned_claim_is_reclaimed(
    world: World, pg_dsn: str
) -> None:
    await world.scheduler.schedule(timer(world, due_in=5))
    await world.db.close()  # the whole process dies with the timer pending

    reborn = await PostgresDatabase.connect(pg_dsn)
    try:
        scheduler = PostgresScheduler(reborn, world.clock)
        advance(world, 6)
        (first,) = await scheduler.claim_due("dead-worker", 5, TTL)  # claimed, never completed
        assert await scheduler.claim_due("other", 5, TTL) == []  # hands off while the claim lives
        advance(world, 31)
        (again,) = await scheduler.claim_due("other", 5, TTL)  # the claim expired: re-delivered
        assert again.scheduler_key == first.scheduler_key
    finally:
        await reborn.close()


# --- waiting for a result is not a failure (Phase 2.1) ---


async def test_pending_tool_result_never_exhausts_turn_attempts(
    world: World, api: ApiHandle
) -> None:
    await start(world)
    api.state.fault = {"status_after_effect": 503}
    await worker(world, api, "w1", book_first(), max_turn_attempts=3).process_conversation(KEY)
    api.state.fault = None
    providers = {"http": world.http_provider(api.base_url)}

    for i in range(10):  # the turn worker polls far more often than reconciliation resolves
        run = await worker(
            world, api, f"poll{i}", FakeLLM([]), max_turn_attempts=3
        ).process_conversation(KEY)
        assert run.status == "waiting"
    assert await world.count("turns", "status='PROCESSING'") == 1  # not FAILED
    assert await world.count("inbox_events", "status='CLAIMED'") == 1  # not DEAD
    assert await world.count("outbox_messages") == 0
    assert len(posts(api)) == 1  # and nothing was re-executed

    await world.reconciler("r", providers=providers).run_once()
    await finish_turn(world, api)
    assert await world.count("turns", "status='COMPLETED'") == 1
    assert len(posts(api)) == 1


async def test_llm_failures_still_exhaust_attempts_and_fail_the_turn_closed(world: World) -> None:
    from conversation_agent.core.errors import LLMProviderError

    await world.inbox.insert_if_absent(event("e1", "oi", clock=world.clock))
    for i in range(3):
        run = await world.coordinator(
            f"w{i}", FakeLLM([LLMProviderError("down")]), max_turn_attempts=3
        ).process_conversation(KEY)
    assert run.turns_completed == 1  # the third failure gives up
    assert await world.count("turns", "status='FAILED'") == 1
    assert await world.count("inbox_events", "status='DEAD'") == 1
    assert await world.count("outbox_messages") == 0  # no side effect happened: nothing to report


async def test_a_turn_that_fails_after_a_side_effect_still_notifies_the_contact(
    world: World, api: ApiHandle
) -> None:
    """The booking happened; if the LLM then dies for good the user must not be left in silence."""
    from conversation_agent.core.errors import LLMProviderError

    await start(world)
    for i in range(2):
        llm = (
            FakeLLM([*book()[:1], LLMProviderError("down")])
            if i == 0
            else FakeLLM([LLMProviderError("down")])
        )
        await worker(world, api, f"w{i}", llm, max_turn_attempts=2).process_conversation(KEY)
    assert await world.count("turns", "status='FAILED'") == 1
    assert len(api.state.bookings) == 1
    (row,) = await world.db.pool.fetch("SELECT text FROM outbox_messages")
    assert "carried out" in row["text"] and "Ref:" in row["text"]
