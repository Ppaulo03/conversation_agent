"""The generic delivery safety contract (INV-034, INV-072, INV-073, ADR-023).

Nothing here knows a particular channel: a scripted sender stands in for any of them. What is
proved is what the runtime does with the four things a sender can say (ACCEPTED, QUEUED, FAILED,
UNKNOWN), when a crash leaves a row SENDING, and how long the same idempotency key may be tried
again.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.faults import ChaosFaults, SimulatedCrash
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.models.delivery import DeliveryPolicy
from conversation_agent.core.models.runtime import OutboundMessage, OutboxStatus, SendResult
from postgres.world import TTL, World, event

RETENTION = timedelta(hours=2)
MARGIN = timedelta(minutes=10)
HORIZON = timedelta(minutes=30)
POLICY = DeliveryPolicy(
    idempotency_retention=RETENTION, retry_horizon=HORIZON, safety_margin=MARGIN
)
NO_RETRY = DeliveryPolicy.no_retry(RETENTION, MARGIN)
RETRY_AFTER = timedelta(seconds=5)  # the worker's default backoff after a retryable FAILED

FAILED_RETRYABLE = SendResult(status=OutboxStatus.FAILED, retryable=True)
FAILED_FINAL = SendResult(status=OutboxStatus.FAILED, retryable=False)
UNKNOWN = SendResult(status=OutboxStatus.UNKNOWN)
ACCEPTED = SendResult(status=OutboxStatus.ACCEPTED, provider_message_id="p1")
QUEUED = SendResult(status=OutboxStatus.QUEUED, channel_message_id="h1")
UNKNOWN_WITH_HANDLE = SendResult(status=OutboxStatus.UNKNOWN, channel_message_id="h1")


class Scripted:
    """A sender that says what it is told; the last step repeats. Doubles as a lookup when it
    is given `answers` (what the channel replies to `lookup`; an Exception is raised)."""

    def __init__(self, *steps: SendResult | Exception) -> None:
        self._steps = list(steps)
        self.attempts: list[OutboundMessage] = []

    async def send(self, message: OutboundMessage) -> SendResult:
        self.attempts.append(message)
        step = self._steps.pop(0) if len(self._steps) > 1 else self._steps[0]
        if isinstance(step, Exception):
            raise step
        return step


class WithLookup(Scripted):
    def __init__(self, answer: SendResult | Exception, *steps: SendResult | Exception) -> None:
        super().__init__(*steps)
        self.answer = answer
        self.lookups = 0

    async def lookup(self, message: OutboundMessage) -> SendResult:
        self.lookups += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


async def make_message(world: World) -> None:
    await world.inbox.insert_if_absent(event("e1", clock=world.clock))
    await world.coordinator("c", FakeLLM([text_response("Olá!")])).run_once()


async def row(world: World) -> dict[str, Any]:
    found = await world.db.pool.fetchrow("SELECT * FROM outbox_messages")
    assert found is not None
    return dict(found)


def tick(world: World, delta: timedelta) -> None:
    world.clock.set(world.clock.now() + delta)


async def send(
    world: World,
    sender: Scripted,
    policy: DeliveryPolicy = POLICY,
    owner: str = "s",
    faults: ChaosFaults | None = None,
) -> int:
    worker = world.outbox_worker(owner, faults, sender=sender, policy=policy)
    return await worker.run_once()


def reconciler(world: World, lookup: Any, policy: DeliveryPolicy = POLICY) -> Any:
    return world.outbox_reconciler("r", lookup, policy, poll_after=timedelta(0))


# --------------------------------------------------------------------------- idempotency identity


async def test_every_resend_of_a_delivery_carries_the_same_key_and_payload(world: World) -> None:
    await make_message(world)
    sender = Scripted(FAILED_RETRYABLE, FAILED_RETRYABLE, ACCEPTED)
    for _ in range(3):
        await send(world, sender)
        tick(world, RETRY_AFTER + timedelta(seconds=1))
    assert len(sender.attempts) == 3
    assert len({m.idempotency_key for m in sender.attempts}) == 1
    assert len({(m.text, m.outbox_id, m.conversation_id) for m in sender.attempts}) == 1
    assert (await row(world))["status"] == OutboxStatus.ACCEPTED


# ------------------------------------------------------------------------------------- FAILED


async def test_a_non_retryable_failed_is_terminal(world: World) -> None:
    await make_message(world)
    sender = Scripted(FAILED_FINAL)
    await send(world, sender)
    tick(world, timedelta(minutes=1))
    assert await send(world, sender) == 0  # nothing left to send
    assert len(sender.attempts) == 1
    assert (await row(world))["status"] == OutboxStatus.FAILED


async def test_a_retryable_failed_is_retried_inside_the_horizon(world: World) -> None:
    await make_message(world)
    sender = Scripted(FAILED_RETRYABLE, ACCEPTED)
    await send(world, sender)
    assert (await row(world))["status"] == OutboxStatus.PENDING
    tick(world, timedelta(minutes=5))  # well inside the 30 min horizon
    await send(world, sender)
    assert len(sender.attempts) == 2 and (await row(world))["status"] == OutboxStatus.ACCEPTED


async def test_a_retryable_failed_is_not_retried_after_the_horizon_and_stays_failed(
    world: World,
) -> None:  # the channel PROVED the failure: running out of time does not make it ambiguous
    await make_message(world)
    sender = Scripted(FAILED_RETRYABLE, ACCEPTED)
    await send(world, sender)
    tick(world, HORIZON + timedelta(minutes=1))  # the retry is due, but the horizon is over
    await send(world, sender)
    stored = await row(world)
    assert stored["status"] == OutboxStatus.FAILED  # NOT UNKNOWN
    assert len(sender.attempts) == 1  # the second attempt never reached the channel


async def test_a_retry_that_would_fall_beyond_the_horizon_is_not_scheduled(world: World) -> None:
    await make_message(world)
    sender = Scripted(FAILED_RETRYABLE, FAILED_RETRYABLE)
    await send(world, sender)
    tick(world, HORIZON - timedelta(seconds=3))  # still inside it: the retry is allowed...
    await send(world, sender)  # attempt 2 fails again, and its retry would land past the horizon
    assert len(sender.attempts) == 2
    assert (await row(world))["status"] == OutboxStatus.FAILED  # decided now, not on a later claim


async def test_the_attempt_cap_still_ends_a_retryable_failed_as_failed(world: World) -> None:
    await make_message(world)
    sender = Scripted(FAILED_RETRYABLE)
    for _ in range(8):
        await send(world, sender)
        tick(world, RETRY_AFTER + timedelta(seconds=1))
    stored = await row(world)
    assert stored["status"] == OutboxStatus.FAILED  # the proven failure, not UNKNOWN
    assert len(sender.attempts) == 5  # the store's attempt cap


async def test_with_no_retry_a_retryable_failed_is_sent_once_and_ends_failed(world: World) -> None:
    await make_message(world)
    sender = Scripted(FAILED_RETRYABLE, ACCEPTED)
    await send(world, sender, NO_RETRY)
    tick(world, timedelta(seconds=30))
    await send(world, sender, NO_RETRY)
    assert len(sender.attempts) == 1
    assert (await row(world))["status"] == OutboxStatus.FAILED


# ------------------------------------------------------------------------------ stale SENDING


async def crash_while_sending(world: World, sender: Scripted, policy: DeliveryPolicy) -> None:
    with pytest.raises(SimulatedCrash):  # sent (maybe), the outcome never recorded
        await send(world, sender, policy, "s1", ChaosFaults("C08_during_outbox_send"))
    assert (await row(world))["status"] == OutboxStatus.SENDING


async def test_an_ambiguous_stale_sending_is_resent_with_the_same_key_inside_the_policy(
    world: World,
) -> None:
    await make_message(world)
    sender = Scripted(ACCEPTED)
    await crash_while_sending(world, sender, POLICY)
    tick(world, TTL + timedelta(seconds=1))
    await send(world, sender, POLICY, "s2")
    assert len(sender.attempts) == 2
    assert sender.attempts[0].idempotency_key == sender.attempts[1].idempotency_key
    assert (await row(world))["status"] == OutboxStatus.ACCEPTED


async def test_an_ambiguous_stale_sending_past_the_horizon_becomes_unknown_not_failed(
    world: World,
) -> None:
    await make_message(world)
    sender = Scripted(ACCEPTED)
    await crash_while_sending(world, sender, POLICY)
    tick(world, TTL + HORIZON)
    await send(world, sender, POLICY, "s2")
    stored = await row(world)
    assert stored["status"] == OutboxStatus.UNKNOWN  # we do not know: NOT the FAILED of a proof
    assert len(sender.attempts) == 1


async def test_an_ambiguous_stale_sending_after_a_proven_failure_stays_ambiguous(
    world: World,
) -> (
    None
):  # the earlier FAILED is proof about attempt 1 only; attempt 2 may have reached the channel
    await make_message(world)
    sender = Scripted(FAILED_RETRYABLE, ACCEPTED)
    await send(world, sender)
    tick(world, timedelta(minutes=1))
    with pytest.raises(SimulatedCrash):
        await send(world, sender, POLICY, "s1", ChaosFaults("C08_during_outbox_send"))
    tick(world, TTL + HORIZON)  # the horizon is over by the time the row is recovered
    await send(world, sender, POLICY, "s2")
    assert (await row(world))["status"] == OutboxStatus.UNKNOWN
    assert len(sender.attempts) == 2


async def test_with_no_retry_a_stale_sending_is_never_sent_again(world: World) -> None:
    await make_message(world)
    sender = Scripted(ACCEPTED)
    await crash_while_sending(world, sender, NO_RETRY)
    tick(world, TTL + timedelta(seconds=1))
    await send(world, sender, NO_RETRY, "s2")
    assert (await row(world))["status"] == OutboxStatus.UNKNOWN
    assert len(sender.attempts) == 1


async def test_no_resend_leaves_the_safe_window_even_with_a_horizon_as_long_as_it(
    world: World,
) -> None:
    full = DeliveryPolicy(
        idempotency_retention=RETENTION, retry_horizon=RETENTION - MARGIN, safety_margin=MARGIN
    )
    await make_message(world)
    sender = Scripted(ACCEPTED)
    await crash_while_sending(world, sender, full)
    tick(world, RETENTION)  # the channel may have forgotten the key
    await send(world, sender, full, "s2")
    assert (await row(world))["status"] == OutboxStatus.UNKNOWN and len(sender.attempts) == 1


# ------------------------------------------------------------------------------------ UNKNOWN


async def test_an_exception_out_of_the_sender_is_unknown_never_failed(world: World) -> None:
    await make_message(world)
    sender = Scripted(RuntimeError("connection reset"))
    await send(world, sender)
    stored = await row(world)
    assert stored["status"] == OutboxStatus.UNKNOWN and stored["channel_message_id"] is None


async def test_unknown_without_a_handle_is_resent_with_the_same_key_when_the_policy_permits(
    world: World,
) -> None:
    await make_message(world)
    sender = Scripted(UNKNOWN, ACCEPTED)
    await send(world, sender)
    tick(world, RETRY_AFTER + timedelta(seconds=1))
    await reconciler(world, None).run_once()  # no lookup needed: there is no handle to ask about
    stored = await row(world)
    assert stored["status"] == OutboxStatus.PENDING
    assert stored["resend_authorized_until"] is not None
    await send(world, sender, POLICY, "s2")
    assert len(sender.attempts) == 2
    assert sender.attempts[0].idempotency_key == sender.attempts[1].idempotency_key
    assert (await row(world))["status"] == OutboxStatus.ACCEPTED


async def test_with_no_retry_unknown_without_a_handle_is_never_resent(world: World) -> None:
    await make_message(world)
    sender = Scripted(UNKNOWN, ACCEPTED)
    await send(world, sender, NO_RETRY)
    tick(world, RETRY_AFTER + timedelta(seconds=1))
    await reconciler(world, None, NO_RETRY).run_once()
    stored = await row(world)
    assert stored["status"] == OutboxStatus.UNKNOWN  # observable, unresolved
    assert stored["last_error"] == "RESEND_NOT_PERMITTED"
    assert await send(world, sender, NO_RETRY, "s2") == 0 and len(sender.attempts) == 1


async def test_unknown_without_a_handle_is_not_resent_past_the_safe_window(world: World) -> None:
    await make_message(world)
    sender = Scripted(UNKNOWN, ACCEPTED)
    await send(world, sender)
    tick(world, RETENTION)
    await reconciler(world, None).run_once()
    stored = await row(world)
    assert stored["status"] == OutboxStatus.UNKNOWN
    assert stored["last_error"] == "UNPROVEN_PAST_RETENTION"
    assert await send(world, sender, POLICY, "s2") == 0 and len(sender.attempts) == 1


# ----------------------------------------------------------------------- QUEUED and settlement


async def test_a_queued_row_is_never_resent_and_waits_for_a_push_without_lookup(
    world: World,
) -> None:
    await make_message(world)
    sender = Scripted(QUEUED)
    await send(world, sender)
    stored = await row(world)
    assert stored["status"] == OutboxStatus.QUEUED and stored["channel_message_id"] == "h1"

    tick(world, RETENTION * 2)  # far past everything
    assert await send(world, sender, POLICY, "s2") == 0  # the worker has nothing to send
    assert await reconciler(world, None).run_once() == 0  # and no lookup: nothing to ask
    assert (await row(world))["status"] == OutboxStatus.QUEUED and len(sender.attempts) == 1

    pushed = SendResult(status=OutboxStatus.ACCEPTED, channel_message_id="h1")
    assert await world.outbox.apply_channel_status("tenant-1", "h1", pushed)
    assert (await row(world))["status"] == OutboxStatus.ACCEPTED


async def test_a_lookup_settles_a_queued_row_as_accepted(world: World) -> None:
    await make_message(world)
    channel = WithLookup(SendResult(status=OutboxStatus.ACCEPTED, channel_message_id="h1"), QUEUED)
    await send(world, channel)
    assert await reconciler(world, channel).run_once() == 1
    assert (await row(world))["status"] == OutboxStatus.ACCEPTED
    assert channel.lookups == 1 and len(channel.attempts) == 1


async def test_a_lookup_settles_an_unknown_row_with_a_handle_as_failed(world: World) -> None:
    await make_message(world)
    channel = WithLookup(
        SendResult(status=OutboxStatus.FAILED, channel_message_id="h1"), UNKNOWN_WITH_HANDLE
    )
    await send(world, channel)
    tick(world, RETRY_AFTER + timedelta(seconds=1))
    await reconciler(world, channel).run_once()
    assert (await row(world))["status"] == OutboxStatus.FAILED
    assert len(channel.attempts) == 1  # settled by asking, not by sending


async def test_a_lookup_that_cannot_answer_leaves_the_row_unsettled_and_visible(
    world: World,
) -> None:
    await make_message(world)
    channel = WithLookup(RuntimeError("channel down"), UNKNOWN_WITH_HANDLE)
    await send(world, channel)
    tick(world, RETRY_AFTER + timedelta(seconds=1))
    await reconciler(world, channel).run_once()
    stored = await row(world)
    assert stored["status"] == OutboxStatus.UNKNOWN and stored["last_error"] == "LOOKUP_FAILED"
    assert len(channel.attempts) == 1


async def test_unknown_with_a_handle_but_no_lookup_is_not_resent_and_waits_for_the_push(
    world: World,
) -> None:
    await make_message(world)
    sender = Scripted(UNKNOWN_WITH_HANDLE)
    await send(world, sender)
    tick(world, RETRY_AFTER + timedelta(seconds=1))
    assert await reconciler(world, None).run_once() == 0  # nothing it can do about a handle
    assert (await row(world))["status"] == OutboxStatus.UNKNOWN
    tick(world, RETENTION * 2)
    assert await send(world, sender, POLICY, "s2") == 0 and len(sender.attempts) == 1

    pushed = SendResult(status=OutboxStatus.ACCEPTED, channel_message_id="h1")
    assert await world.outbox.apply_channel_status("tenant-1", "h1", pushed)
    assert (await row(world))["status"] == OutboxStatus.ACCEPTED


async def test_a_pushed_status_only_moves_forward(world: World) -> None:
    await make_message(world)
    await send(world, Scripted(QUEUED))
    final = SendResult(status=OutboxStatus.ACCEPTED, channel_message_id="h1")
    assert await world.outbox.apply_channel_status("tenant-1", "h1", final)
    for late in (QUEUED, SendResult(status=OutboxStatus.FAILED, channel_message_id="h1")):
        assert not await world.outbox.apply_channel_status("tenant-1", "h1", late)
    assert (await row(world))["status"] == OutboxStatus.ACCEPTED


@pytest.mark.parametrize("sent", [QUEUED, UNKNOWN_WITH_HANDLE])
@pytest.mark.parametrize("pushed", [OutboxStatus.ACCEPTED, OutboxStatus.FAILED])
async def test_a_push_settles_a_row_left_reconciling_by_a_reconciler_that_disappeared(
    world: World, sent: SendResult, pushed: OutboxStatus
) -> None:
    await make_message(world)
    await send(world, Scripted(sent))
    tick(world, RETRY_AFTER + timedelta(seconds=1))
    (claimed,) = await world.outbox.claim_unsettled("gone", 5, TTL, timedelta(0))
    assert claimed.channel_message_id == "h1"
    assert (await row(world))["status"] == OutboxStatus.RECONCILING  # and its owner never returns

    final = SendResult(status=pushed, channel_message_id="h1")
    assert await world.outbox.apply_channel_status("tenant-1", "h1", final)
    stored = await row(world)
    assert stored["status"] == pushed and stored["claim_owner"] is None

    for late in (QUEUED, SendResult(status=OutboxStatus.ACCEPTED, channel_message_id="h1")):
        assert not await world.outbox.apply_channel_status("tenant-1", "h1", late)  # monotonic
    assert (await row(world))["status"] == pushed
