"""RUNTIME_PROTOCOL §4 (A prepare / B execute / C1 finalize fact / C2 apply) on PostgreSQL,
with a real write against the reference API. Chaos: C07, C14, C16 (+ C04 handoff to recovery).
INV-011, INV-015, INV-016, INV-021 (same idempotency key), "no I/O inside a transaction".
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from typing import Any

import pytest

from conftest import ApiHandle
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.faults import ChaosFaults, SimulatedCrash
from conversation_agent.adapters.llm.fake import FakeLLM, text_response, tool_call_response
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.errors import ExecutionFencingError, FencingError
from conversation_agent.core.models.runtime import InvocationStatus, ToolInvocation
from conversation_agent.core.models.tooling import (
    CapabilityRequest,
    ToolContext,
    ToolError,
    ToolResult,
)
from conversation_agent.ports.llm import LLMRequest
from postgres.world import KEY, TTL, AllowWrites, World, event
from support.builders import IDENTITY

BOOKING_ARGS = {
    "service_id": "haircut",
    "start_at": "2026-10-06T10:00:00-03:00",
    "duration_minutes": 30,
}


def book() -> list[Any]:
    return [tool_call_response("scheduling__create", BOOKING_ARGS), text_response("Agendado!")]


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


def posts(api: ApiHandle) -> list[dict[str, object]]:
    return [r for r in api.requests if r["method"] == "POST"]


async def say_book(world: World) -> None:
    await world.inbox.insert_if_absent(event("e1", "quero terça 10h", clock=world.clock))


def worker(world: World, api: ApiHandle, owner: str, llm: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
    return world.coordinator(
        owner,
        llm,
        providers={"http": world.http_provider(api.base_url)},
        policy=AllowWrites(frozenset({"scheduling.availability", "scheduling.create"})),
        **kwargs,
    )


async def invocation(world: World) -> ToolInvocation:
    inv = await world.ledger.get(
        KEY.tenant_id, await world.db.pool.fetchval("SELECT invocation_id FROM tool_invocations")
    )
    assert inv is not None
    return inv


# --- happy path -------------------------------------------------------------------------------


async def test_a_write_runs_the_full_protocol_once_with_a_stable_idempotency_key(
    world: World, api: ApiHandle
) -> None:
    await say_book(world)
    run = await worker(world, api, "w1", FakeLLM(book())).process_conversation(KEY)
    assert (run.status, run.turns_completed) == ("done", 1)

    inv = await invocation(world)
    assert inv.status is InvocationStatus.SUCCEEDED
    assert inv.result_application_status == "applied"
    assert inv.execution_epoch == 1
    assert inv.idempotency_key == inv.invocation_id == inv.context.invocation_id

    (post,) = posts(api)  # exactly one real booking, carrying the invocation identity as key
    assert post["headers"]["idempotency-key"] == inv.invocation_id  # type: ignore[index]
    assert len(api.state.bookings) == 1

    steps = [
        r["step_type"]
        for r in await world.db.pool.fetch("SELECT step_type FROM turn_journal ORDER BY step_index")
    ]
    assert steps == [
        "INBOUND_AGGREGATED", "LLM_REQUEST", "LLM_RESPONSE", "CAPABILITY_REQUEST",
        "POLICY_DECISION", "TOOL_PREPARED", "TOOL_RESULT", "LLM_REQUEST", "LLM_RESPONSE",
        "TURN_COMPLETED",
    ]  # fmt: skip
    assert await world.count("outbox_messages") == 1


async def test_without_a_confirmation_policy_a_write_is_only_a_draft_never_executed(
    world: World, api: ApiHandle
) -> None:
    """The default PolicyGate still holds protected capabilities back (Phase 3 gates them)."""
    await say_book(world)
    run = await world.coordinator(
        "w1", FakeLLM(book()), providers={"http": world.http_provider(api.base_url)}
    ).process_conversation(KEY)
    assert run.status == "done"
    assert posts(api) == [] and await world.count("tool_invocations") == 0


async def test_business_error_is_a_terminal_failed_fact_that_is_never_reopened(
    world: World, api: ApiHandle
) -> None:  # INV-015
    api.state.fully_booked_dates = {date(2026, 10, 6)}
    seen: dict[str, Any] = {}

    def observe(request: LLMRequest) -> Any:
        parts = [p for m in request.messages for p in m.parts if hasattr(p, "tool_call_id")]
        seen.update(json.loads(parts[-1].content))  # type: ignore[attr-defined]
        return text_response("Esse horário acabou de ser ocupado.")

    await say_book(world)
    llm = FakeLLM([tool_call_response("scheduling__create", BOOKING_ARGS), observe])
    await worker(world, api, "w1", llm).process_conversation(KEY)

    assert seen["status"] == "business_error" and seen["error"]["code"] == "SLOT_UNAVAILABLE"
    inv = await invocation(world)
    assert inv.status is InvocationStatus.FAILED and inv.result_application_status == "applied"
    assert await world.ledger.claim_execution(inv.tenant_id, inv.invocation_id, "x", TTL) is None
    # a new *semantic* attempt would be a new invocation (new identity), never this row again
    same = await world.db.pool.fetchval("SELECT status FROM tool_invocations")
    assert same == "FAILED"


async def test_no_database_transaction_is_open_during_llm_or_tool_io(
    world: World, api: ApiHandle
) -> None:
    open_transactions: list[int] = []

    async def probe() -> None:
        n = await world.db.pool.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND state LIKE 'idle in transaction%'"
        )
        open_transactions.append(int(n))

    class ProbeLLM(FakeLLM):
        async def complete(self, request: LLMRequest):  # type: ignore[no-untyped-def]
            await probe()
            return await super().complete(request)

    class ProbeProvider:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        async def execute(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            await probe()
            return await self._inner.execute(*args, **kwargs)

    await say_book(world)
    coordinator = world.coordinator(
        "w1",
        ProbeLLM(book()),
        providers={"http": ProbeProvider(world.http_provider(api.base_url))},
        policy=AllowWrites(frozenset({"scheduling.availability", "scheduling.create"})),
    )
    await coordinator.process_conversation(KEY)
    assert len(open_transactions) == 3 and set(open_transactions) == {0}  # 2 LLM + 1 tool


# --- chaos ---------------------------------------------------------------------------------------


async def test_C07_after_ledger_finalize_new_owner_applies_without_repeating_the_tool(
    world: World, api: ApiHandle
) -> None:
    await say_book(world)
    crash = ChaosFaults("C07_after_ledger_finalize")
    with pytest.raises(SimulatedCrash):
        await worker(world, api, "w1", FakeLLM(book()), faults=crash).process_conversation(KEY)

    inv = await invocation(world)  # C1 is durable, C2 never happened
    assert inv.status is InvocationStatus.SUCCEEDED and inv.result_application_status == "pending"
    assert await world.count("turn_journal", "step_type='TOOL_RESULT'") == 0
    assert await world.count("outbox_messages") == 0

    world.clock.set(world.clock.now() + TTL + timedelta(seconds=1))
    heir_llm = FakeLLM([text_response("Agendado!")])  # step 1 comes from the journal
    run = await worker(world, api, "w2", heir_llm).process_conversation(KEY)
    assert (run.status, run.turns_completed) == ("done", 1)

    assert len(posts(api)) == 1 and len(api.state.bookings) == 1  # the tool did not repeat
    assert (await invocation(world)).result_application_status == "applied"
    assert await world.count("outbox_messages") == 1  # the contact receives the reply
    assert heir_llm.calls == 1


async def test_C14_lease_lost_after_external_success_ledger_keeps_the_fact_conversation_untouched(
    world: World, api: ApiHandle
) -> None:
    class GatedProvider:
        def __init__(self, inner: Any) -> None:
            self._inner = inner
            self.started, self.release = asyncio.Event(), asyncio.Event()

        async def execute(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            self.started.set()
            await self.release.wait()
            return await self._inner.execute(*args, **kwargs)

    gate = GatedProvider(world.http_provider(api.base_url))
    await say_book(world)
    zombie = world.coordinator(
        "zombie",
        FakeLLM(book()),
        providers={"http": gate},
        policy=AllowWrites(frozenset({"scheduling.availability", "scheduling.create"})),
    )
    task = asyncio.create_task(zombie.process_conversation(KEY))
    await asyncio.wait_for(gate.started.wait(), 5)  # the zombie is inside the external call

    world.clock.set(world.clock.now() + TTL + timedelta(seconds=1))
    heir_lease = await world.leases.acquire(KEY, "heir", TTL)  # the conversation moves on
    assert heir_lease is not None and heir_lease.epoch == 2

    gate.release.set()  # the call completes in the zombie, which no longer owns the conversation
    result = await asyncio.wait_for(task, 5)
    assert result.status == "stale"

    inv = await invocation(world)  # the external fact was recorded through execution_epoch (C1)
    assert inv.status is InvocationStatus.SUCCEEDED and inv.result_application_status == "pending"
    assert len(api.state.bookings) == 1
    assert await world.count("turn_journal", "step_type='TOOL_RESULT'") == 0  # no C2
    assert await world.count("outbox_messages") == 0
    state = await world.db.pool.fetchval("SELECT state_json FROM conversation_states")
    assert state in ({}, {"history": [], "proposals": {}})  # conversation not mutated

    await world.leases.release(heir_lease)  # the new owner applies C2 without re-executing
    heir = FakeLLM([text_response("Agendado!")])
    run = await worker(world, api, "heir", heir).process_conversation(KEY)
    assert run.status == "done"
    assert len(posts(api)) == 1 and len(api.state.bookings) == 1
    assert (await invocation(world)).result_application_status == "applied"
    assert await world.count("outbox_messages") == 1


async def test_C16_after_apply_before_compose_replay_reproduces_without_repeating_anything(
    world: World, api: ApiHandle
) -> None:
    await say_book(world)
    crash = ChaosFaults("C16_after_apply_before_compose")
    with pytest.raises(SimulatedCrash):
        await worker(world, api, "w1", FakeLLM(book()), faults=crash).process_conversation(KEY)
    assert (await invocation(world)).result_application_status == "applied"  # C2 committed
    assert await world.count("outbox_messages") == 0  # ...but the reply was never composed
    assert await world.count("turns", "status='PROCESSING'") == 1

    world.clock.set(world.clock.now() + TTL + timedelta(seconds=1))
    silent = FakeLLM([])  # any LLM call would fail: everything must come from the journal
    run = await worker(world, api, "w2", silent).process_conversation(KEY)
    assert (run.status, run.turns_completed) == ("done", 1)
    assert silent.calls == 0 and len(posts(api)) == 1
    assert await world.count("outbox_messages") == 1  # created exactly once

    again = await worker(world, api, "w3", FakeLLM([])).process_conversation(KEY)
    assert again.status == "idle" and await world.count("outbox_messages") == 1


async def test_C04_crash_before_the_request_leaves_an_executing_invocation_nobody_re_runs_blindly(
    world: World, api: ApiHandle
) -> None:
    await say_book(world)
    crash = ChaosFaults("C04_before_external_request")
    with pytest.raises(SimulatedCrash):
        await worker(world, api, "w1", FakeLLM(book()), faults=crash).process_conversation(KEY)
    inv = await invocation(world)
    assert inv.status is InvocationStatus.EXECUTING and posts(api) == []

    world.clock.set(world.clock.now() + TTL + timedelta(seconds=1))  # conversation lease expired
    run = await worker(world, api, "w2", FakeLLM([])).process_conversation(KEY)
    assert run.status == "retry_later"  # the outcome is not known: the turn waits (no re-run)
    assert posts(api) == []
    assert await world.count("turns", "status='PROCESSING'") == 1


# --- ledger fences (unit level) ----------------------------------------------------------------


def make_invocation(invocation_id: str = "inv-1") -> ToolInvocation:
    context = ToolContext(
        tenant_id=IDENTITY.tenant_id, agent_id="a", agent_version="1",
        channel_id=IDENTITY.channel_id, conversation_id=IDENTITY.conversation_id,
        session_id=IDENTITY.session_id, contact_id=IDENTITY.contact_id, turn_id="t1",
        invocation_id=invocation_id, trace_id="tr",
    )  # fmt: skip
    return ToolInvocation(
        tenant_id=IDENTITY.tenant_id,
        invocation_id=invocation_id,
        conversation_id=IDENTITY.conversation_id,
        session_id=IDENTITY.session_id,
        turn_id="t1",
        logical_step_id="t1:3",
        attempt_semantic_id="t1:3:a1",
        tool_name="erp_create_reservation",
        capability="scheduling.create",
        args_hash="h",
        idempotency_key=invocation_id,
        request=CapabilityRequest(capability="scheduling.create", args={}, args_hash="h"),
        context=context,
    )


async def prepared(world: World) -> tuple[Any, ToolInvocation]:
    await world.inbox.insert_if_absent(event("e0", clock=world.clock))
    lease = await world.leases.acquire(KEY, "owner", TTL)
    assert lease is not None
    async with world.uows.begin(lease.fence) as uow:
        inv = await uow.invocations.create_prepared(make_invocation())
        await uow.commit()
    return lease, inv


OK = ToolResult(status="success", data={"booking_id": "b1", "status": "confirmed"})


async def test_prepare_is_idempotent_and_claim_only_works_from_prepared(world: World) -> None:
    lease, inv = await prepared(world)
    async with world.uows.begin(lease.fence) as uow:  # replayed PREPARE: same row, no duplicate
        again = await uow.invocations.create_prepared(make_invocation())
        await uow.commit()
    assert again.invocation_id == inv.invocation_id and await world.count("tool_invocations") == 1

    claim = await world.ledger.claim_execution(inv.tenant_id, inv.invocation_id, "ex1", TTL)
    assert claim is not None and claim.epoch == 1
    assert await world.ledger.claim_execution(inv.tenant_id, inv.invocation_id, "ex2", TTL) is None


async def test_finalize_is_fenced_by_execution_epoch(world: World) -> None:  # INV-016
    _, inv = await prepared(world)
    claim = await world.ledger.claim_execution(inv.tenant_id, inv.invocation_id, "ex1", TTL)
    assert claim is not None
    stale = claim.model_copy(update={"epoch": claim.epoch + 7})
    with pytest.raises(ExecutionFencingError):
        await world.ledger.finalize_execution(inv.tenant_id, inv.invocation_id, stale, OK)
    done = await world.ledger.finalize_execution(inv.tenant_id, inv.invocation_id, claim, OK)
    assert done.status is InvocationStatus.SUCCEEDED and done.result_application_status == "pending"
    with pytest.raises(ExecutionFencingError):  # a terminal fact cannot be overwritten
        await world.ledger.finalize_execution(inv.tenant_id, inv.invocation_id, claim, OK)


async def test_lost_conversation_lease_can_finalize_ledger_only(world: World) -> None:  # INV-011
    lease, inv = await prepared(world)
    claim = await world.ledger.claim_execution(inv.tenant_id, inv.invocation_id, "ex1", TTL)
    assert claim is not None
    world.clock.set(world.clock.now() + TTL + timedelta(seconds=1))
    assert await world.leases.acquire(KEY, "someone-else", TTL) is not None  # lease lost

    done = await world.ledger.finalize_execution(inv.tenant_id, inv.invocation_id, claim, OK)
    assert done.status is InvocationStatus.SUCCEEDED  # the fact is still recorded...
    with pytest.raises(FencingError):  # ...but applying it to the conversation is refused
        async with world.uows.begin(lease.fence) as uow:
            await uow.invocations.mark_applied(inv.invocation_id, world.clock.now())
            await uow.commit()
    assert (
        await world.ledger.get(inv.tenant_id, inv.invocation_id)
    ).result_application_status == "pending"  # type: ignore[union-attr]


async def test_execution_epoch_and_conversation_epoch_have_disjoint_write_boundaries(
    world: World,
) -> None:  # INV-016
    lease, inv = await prepared(world)
    before = await world.db.pool.fetchrow(
        "SELECT version, conversation_epoch FROM conversation_states"
    )
    claim = await world.ledger.claim_execution(inv.tenant_id, inv.invocation_id, "ex1", TTL)
    assert claim is not None
    await world.ledger.finalize_execution(inv.tenant_id, inv.invocation_id, claim, OK)
    after = await world.db.pool.fetchrow(
        "SELECT version, conversation_epoch FROM conversation_states"
    )
    assert dict(before) == dict(after)  # ledger transitions never touch the conversation row

    epoch_before = await world.db.pool.fetchval("SELECT execution_epoch FROM tool_invocations")
    async with world.uows.begin(lease.fence) as uow:
        await uow.invocations.mark_applied(inv.invocation_id, world.clock.now())
        await uow.commit()
    assert (
        await world.db.pool.fetchval("SELECT execution_epoch FROM tool_invocations") == epoch_before
    )


async def test_unknown_outcome_is_recorded_but_not_applied_until_reconciled(world: World) -> None:
    _, inv = await prepared(world)
    claim = await world.ledger.claim_execution(inv.tenant_id, inv.invocation_id, "ex1", TTL)
    assert claim is not None
    unknown = ToolResult(
        status="unknown", error=ToolError(code="EXTERNAL_TIMEOUT", message_safe="no answer")
    )
    rec = await world.ledger.finalize_execution(inv.tenant_id, inv.invocation_id, claim, unknown)
    assert rec.status is InvocationStatus.UNKNOWN
    assert rec.result_application_status == "none"
    assert await world.ledger.pending_application(inv.tenant_id, inv.conversation_id) == []
