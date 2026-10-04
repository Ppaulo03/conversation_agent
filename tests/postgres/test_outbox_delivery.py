"""Outbound delivery over the gateway (INV-034): the sender stops re-sending before the gateway's
idempotency memory fades, UNKNOWN rows are reconciled by asking the gateway, and absence proves
nothing once the window has passed."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import RELAY_RETENTION, RELAY_T0, RelayHandle
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.connections.static import StaticConnectionResolver
from conversation_agent.adapters.faults import ChaosFaults, SimulatedCrash
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.senders.relayplane import RelayPlaneSender
from conversation_agent.core.models.connections import ResolvedConnection
from conversation_agent.core.models.delivery import DeliveryPolicy
from conversation_agent.core.models.runtime import OutboxStatus
from postgres.world import TTL, World, event

POLICY = DeliveryPolicy(
    idempotency_retention=RELAY_RETENTION,
    retry_horizon=timedelta(minutes=10),
    reconcile_margin=timedelta(minutes=5),
)


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


def gateway(relay: RelayHandle) -> RelayPlaneSender:
    connection = ResolvedConnection(
        connection_id="relayplane",
        base_url=relay.base_url,
        allow_private_networks=True,
        tls_required=False,
    )
    return RelayPlaneSender(StaticConnectionResolver({"relayplane": connection}))


async def make_message(world: World) -> None:
    await world.inbox.insert_if_absent(event("e1", clock=world.clock))
    await world.coordinator("c", FakeLLM([text_response("Olá!")])).run_once()


async def row(world: World) -> dict[str, object]:
    found = await world.db.pool.fetchrow("SELECT * FROM outbox_messages")
    assert found is not None
    return dict(found)


def tick(world: World, relay: RelayHandle, delta: timedelta) -> None:
    """Both clocks move together (the gateway's clock is its own, but time passes for both)."""
    world.clock.set(world.clock.now() + delta)
    relay.advance(delta)


async def test_delivery_over_the_gateway_records_the_channels_acceptance_time(
    world: World, relay: RelayHandle
) -> None:
    relay.set_time(RELAY_T0 + timedelta(hours=3))  # the channel's own clock
    await make_message(world)
    assert await world.outbox_worker("s", sender=gateway(relay)).run_once() == 1
    stored = await row(world)
    assert stored["status"] == OutboxStatus.ACCEPTED
    assert stored["provider_accepted_at"] == RELAY_T0 + timedelta(hours=3)  # never our own clock
    assert len(relay.deliveries) == 1 and stored["first_sent_at"] is not None


async def test_a_lost_answer_is_reconciled_by_lookup_without_a_second_delivery(
    world: World, relay: RelayHandle
) -> None:
    await make_message(world)
    relay.state.fault = {"status_after_effect": 503}  # taken by the gateway, answer lost
    await world.outbox_worker("s", sender=gateway(relay)).run_once()
    assert (await row(world))["status"] == OutboxStatus.UNKNOWN and len(relay.deliveries) == 1

    relay.state.fault = None
    assert await world.outbox_reconciler("r", gateway(relay), POLICY).run_once() == 1
    stored = await row(world)
    assert stored["status"] == OutboxStatus.ACCEPTED and stored["reconcile_attempts"] == 1
    assert len(relay.deliveries) == 1  # nothing was re-sent


async def test_a_message_the_gateway_never_got_is_resent_with_the_same_key(
    world: World, relay: RelayHandle
) -> None:
    await make_message(world)
    key = (await row(world))["idempotency_key"]
    relay.state.fault = {"status": 503}  # refused before any effect, but the sender cannot know
    await world.outbox_worker("s", sender=gateway(relay)).run_once()
    assert (await row(world))["status"] == OutboxStatus.UNKNOWN and relay.deliveries == []

    relay.state.fault = None
    await world.outbox_reconciler("r", gateway(relay), POLICY).run_once()  # 404: proven absent
    assert (await row(world))["status"] == OutboxStatus.PENDING
    await world.outbox_worker("s2", sender=gateway(relay)).run_once()
    stored = await row(world)
    assert stored["status"] == OutboxStatus.ACCEPTED and len(relay.deliveries) == 1
    assert relay.deliveries[0]["idempotency_key"] == key  # the SAME key as the first attempt


async def test_an_unanswerable_lookup_proves_nothing_and_resends_nothing(
    world: World, relay: RelayHandle
) -> None:
    await make_message(world)
    relay.state.fault = {"status": 503}
    await world.outbox_worker("s", sender=gateway(relay)).run_once()
    await world.outbox_reconciler("r", gateway(relay), POLICY).run_once()  # lookup also fails
    stored = await row(world)
    assert stored["status"] == OutboxStatus.UNKNOWN and stored["last_error"] == "LOOKUP_FAILED"
    assert relay.deliveries == []


async def test_the_sender_does_not_resend_blindly_past_the_retry_horizon(
    world: World, relay: RelayHandle
) -> None:
    await make_message(world)
    with pytest.raises(SimulatedCrash):  # delivered to the gateway, never recorded
        await world.outbox_worker(
            "s1", ChaosFaults("C08_during_outbox_send"), sender=gateway(relay)
        ).run_once()
    assert len(relay.deliveries) == 1

    tick(world, relay, TTL + timedelta(minutes=11))  # claim expired AND past the 10 min horizon
    worker = world.outbox_worker("s2", sender=gateway(relay), retry_horizon=POLICY.retry_horizon)
    assert await worker.run_once() == 1
    stored = await row(world)
    assert stored["status"] == OutboxStatus.UNKNOWN and stored["attempts"] == 2
    assert len(relay.state.requests) == 1  # the second attempt NEVER reached the wire

    await world.outbox_reconciler("r", gateway(relay), POLICY).run_once()
    assert (await row(world))["status"] == OutboxStatus.ACCEPTED  # the gateway still remembers
    assert len(relay.deliveries) == 1


async def test_inside_the_horizon_a_stale_row_is_still_retried_with_the_same_key(
    world: World, relay: RelayHandle
) -> None:
    await make_message(world)
    with pytest.raises(SimulatedCrash):
        await world.outbox_worker(
            "s1", ChaosFaults("C08_during_outbox_send"), sender=gateway(relay)
        ).run_once()
    tick(world, relay, TTL + timedelta(seconds=1))  # well inside the horizon
    worker = world.outbox_worker("s2", sender=gateway(relay), retry_horizon=POLICY.retry_horizon)
    assert await worker.run_once() == 1
    assert (await row(world))["status"] == OutboxStatus.ACCEPTED
    assert len(relay.deliveries) == 1  # deduped by the key: the retry was safe


async def test_past_the_retention_an_absent_lookup_is_not_proof_and_nothing_is_resent(
    world: World, relay: RelayHandle
) -> None:
    await make_message(world)
    relay.state.fault = {"status_after_effect": 503}
    await world.outbox_worker("s", sender=gateway(relay)).run_once()
    assert len(relay.deliveries) == 1

    relay.state.fault = None
    tick(world, relay, RELAY_RETENTION + timedelta(minutes=10))  # the gateway forgot the key
    await world.outbox_reconciler("r", gateway(relay), POLICY).run_once()
    stored = await row(world)
    assert stored["status"] == OutboxStatus.UNKNOWN
    assert stored["last_error"] == "UNPROVEN_PAST_RETENTION"
    assert len(relay.deliveries) == 1  # a resend would have duplicated the message


async def test_two_reconcilers_cannot_both_resolve_the_same_row(
    world: World, relay: RelayHandle
) -> None:
    await make_message(world)
    relay.state.fault = {"status_after_effect": 503}
    await world.outbox_worker("s", sender=gateway(relay)).run_once()
    relay.state.fault = None
    first = world.outbox_reconciler("r1", gateway(relay), POLICY)
    second = world.outbox_reconciler("r2", gateway(relay), POLICY)
    claimed_a = await world.outbox.claim_unknown("r1", 10, TTL)
    claimed_b = await world.outbox.claim_unknown("r2", 10, TTL)
    assert len(claimed_a) == 1 and claimed_b == []  # the claim is exclusive
    assert first is not second
