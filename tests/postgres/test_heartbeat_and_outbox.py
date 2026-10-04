"""Lease heartbeat (DoD) and Outbox delivery semantics, incl. C08_during_outbox_send."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.faults import ChaosFaults, SimulatedCrash
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.errors import StaleWorkerError
from conversation_agent.core.models.runtime import OutboxStatus, SendResult
from conversation_agent.engine.heartbeat import LeaseHandle
from postgres.world import KEY, TTL, World, event


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


async def new_handle(world: World, interval: float) -> LeaseHandle:
    await world.inbox.insert_if_absent(event("e0", clock=world.clock))  # first contact
    lease = await world.leases.acquire(KEY, "w1", TTL)
    assert lease is not None
    return LeaseHandle(lease, world.leases, world.coord, ttl=TTL, interval_seconds=interval)


# --- heartbeat ------------------------------------------------------------------------------


async def test_heartbeat_prevents_silent_expiry_during_a_long_operation(world: World) -> None:
    await world.inbox.insert_if_absent(event("e1", clock=world.clock))
    handle = await new_handle(world, 0.05)
    await handle.start()
    try:
        for _ in range(4):  # 80s of "LLM/tool time" against a 30s TTL
            world.clock.set(world.clock.now() + timedelta(seconds=20))
            await asyncio.sleep(0.25)
            assert await world.leases.acquire(KEY, "rival", TTL) is None
            handle.ensure_active()
    finally:
        await handle.stop()
    assert not handle.stale


async def test_lost_heartbeat_makes_the_worker_stale_and_stops_new_steps(world: World) -> None:
    await world.inbox.insert_if_absent(event("e1", clock=world.clock))
    handle = await new_handle(world, 0.05)
    await handle.start()
    try:
        await world.db.pool.execute(  # someone else took the conversation over
            "UPDATE conversation_states SET lease_owner='thief', conversation_epoch = 99"
        )
        await asyncio.sleep(0.3)
        assert handle.stale
        with pytest.raises(StaleWorkerError):
            handle.ensure_active()
    finally:
        await handle.stop()


async def test_local_expiry_without_a_successful_heartbeat_is_stale(world: World) -> None:
    handle = await new_handle(world, 5.0)  # heartbeat never fires during the test
    handle.ensure_active()
    world.clock.set(world.clock.now() + TTL + timedelta(seconds=1))
    with pytest.raises(StaleWorkerError):
        handle.ensure_active()


async def test_heartbeat_interval_must_be_shorter_than_the_ttl(world: World) -> None:
    await world.inbox.insert_if_absent(event("e0", clock=world.clock))
    lease = await world.leases.acquire(KEY, "w1", TTL)
    assert lease is not None
    with pytest.raises(ValueError):
        LeaseHandle(lease, world.leases, world.coord, ttl=TTL, interval_seconds=30)


async def test_a_stale_worker_starts_no_new_step(world: World) -> None:
    """The turn stops at the safe boundary: no tool runs after the lease is lost."""
    await world.inbox.insert_if_absent(event("e1", clock=world.clock))

    class StealingLLM:
        async def complete(self, request):  # type: ignore[no-untyped-def]
            from support.builders import availability_call

            await world.db.pool.execute(
                "UPDATE conversation_states SET lease_owner='thief', conversation_epoch = 99"
            )
            await asyncio.sleep(0.3)  # heartbeat notices
            return availability_call("haircut", "2026-10-06")

    coordinator = world.coordinator("w1", StealingLLM(), heartbeat_interval_seconds=0.05)
    run = await coordinator.process_conversation(KEY)
    assert run.status == "stale"
    assert world.tools.calls == []
    assert await world.count("outbox_messages") == 0


# --- outbox -----------------------------------------------------------------------------------


async def make_message(world: World) -> None:
    await world.inbox.insert_if_absent(event("e1", clock=world.clock))
    await world.coordinator("c", FakeLLM([text_response("Olá!")])).run_once()


async def outbox_row(world: World) -> dict[str, object]:
    row = await world.db.pool.fetchrow("SELECT * FROM outbox_messages")
    assert row is not None
    return dict(row)


async def test_C08_crash_during_send_resends_same_payload_and_key_without_duplicating(
    world: World,
) -> None:
    await make_message(world)
    original = await outbox_row(world)
    crashing = world.outbox_worker("s1", ChaosFaults("C08_during_outbox_send"))
    with pytest.raises(SimulatedCrash):
        await crashing.run_once()  # delivered to the channel, but never marked sent
    assert len(world.sender.delivered) == 1
    assert (await outbox_row(world))["status"] == OutboxStatus.SENDING

    assert await world.outbox_worker("s2").run_once() == 0  # claim still valid: hands off

    world.clock.set(world.clock.now() + TTL + timedelta(seconds=1))  # the sender is declared dead
    assert await world.outbox_worker("s2").run_once() == 1
    row = await outbox_row(world)
    assert row["status"] == OutboxStatus.ACCEPTED
    assert row["text"] == original["text"]
    assert row["idempotency_key"] == original["idempotency_key"]
    assert [m.idempotency_key for m in world.sender.attempts] == [original["idempotency_key"]] * 2
    assert len(world.sender.delivered) == 1  # the channel deduped the retry


async def test_retryable_failure_backs_off_then_succeeds(world: World) -> None:
    await make_message(world)
    world.sender.next_result = SendResult(status=OutboxStatus.FAILED, retryable=True)
    worker = world.outbox_worker("s1")
    assert await worker.run_once() == 1
    row = await outbox_row(world)
    assert row["status"] == OutboxStatus.PENDING and row["attempts"] == 1
    assert await worker.run_once() == 0  # not due yet

    world.sender.next_result = None
    world.sender._by_key.clear()
    world.clock.set(world.clock.now() + timedelta(seconds=6))
    assert await worker.run_once() == 1
    assert (await outbox_row(world))["status"] == OutboxStatus.ACCEPTED


async def test_non_retryable_failure_is_terminal(world: World) -> None:
    await make_message(world)
    world.sender.next_result = SendResult(status=OutboxStatus.FAILED, retryable=False)
    await world.outbox_worker("s1").run_once()
    assert (await outbox_row(world))["status"] == OutboxStatus.FAILED
    world.clock.set(world.clock.now() + timedelta(hours=1))
    assert await world.outbox_worker("s1").run_once() == 0


async def test_sender_exception_is_unknown_never_a_blind_retry(world: World) -> None:
    await make_message(world)
    world.sender.fail_next = ConnectionError("connection reset after send")
    await world.outbox_worker("s1").run_once()
    assert (await outbox_row(world))["status"] == OutboxStatus.UNKNOWN
    world.clock.set(world.clock.now() + timedelta(hours=1))
    assert await world.outbox_worker("s1").run_once() == 0  # needs reconciliation, not resend


async def test_a_sender_that_lost_its_claim_cannot_overwrite_the_newer_outcome(
    world: World,
) -> None:
    await make_message(world)
    (stale_claim,) = await world.outbox.claim_ready("slow", 5, TTL)
    world.clock.set(world.clock.now() + TTL + timedelta(seconds=1))
    (fresh_claim,) = await world.outbox.claim_ready("fast", 5, TTL)
    ok = SendResult(status=OutboxStatus.ACCEPTED, provider_message_id="p1")
    await world.outbox.record_result(fresh_claim, "fast", ok, timedelta(seconds=5))

    late = SendResult(status=OutboxStatus.FAILED)
    await world.outbox.record_result(stale_claim, "slow", late, timedelta(seconds=5))
    assert (await outbox_row(world))["status"] == OutboxStatus.ACCEPTED
