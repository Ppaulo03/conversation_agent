"""Outbound delivery over the gateway (INV-034), following its contract: a send is acknowledged
as QUEUED with the gateway's own id; ACCEPTED (with the provider id and acceptance time) arrives
later, by event or by asking; a resend of the SAME key is a replay inside the gateway's
idempotency window and a possible duplicate outside it."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import RELAY_RETENTION, RELAY_T0, RelayHandle
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.connections.static import StaticConnectionResolver
from conversation_agent.adapters.faults import ChaosFaults, SimulatedCrash
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.senders.relayplane import RelayPlaneSender, result_from_gateway
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


def reconciler(world: World, relay: RelayHandle, owner: str = "r"):  # type: ignore[no-untyped-def]
    return world.outbox_reconciler(owner, gateway(relay), POLICY, poll_after=timedelta(0))


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


async def send(world: World, relay: RelayHandle, owner: str = "s", **kw: object) -> None:
    await world.outbox_worker(owner, sender=gateway(relay), **kw).run_once()  # type: ignore[arg-type]


async def test_a_send_is_queued_until_the_provider_reports_it_accepted(
    world: World, relay: RelayHandle
) -> None:
    await make_message(world)
    await send(world, relay)
    stored = await row(world)
    assert stored["status"] == OutboxStatus.QUEUED  # accepted durably, NOT yet by the provider
    assert stored["channel_message_id"] == "msg_1" and stored["provider_message_id"] is None
    assert stored["first_sent_at"] is not None and len(relay.sent) == 1


async def test_the_accepted_event_gives_the_provider_id_and_the_channels_acceptance_time(
    world: World, relay: RelayHandle
) -> None:
    await make_message(world)
    await send(world, relay)
    accepted_at = (RELAY_T0 + timedelta(hours=3)).isoformat()
    applied = await world.outbox.apply_channel_status(
        "tenant-1",
        "msg_1",
        result_from_gateway(
            "ACCEPTED",
            channel_message_id="msg_1",
            provider_message_id="3EB0316FBDC6EC84F13164",
            accepted_at=accepted_at,
        ),  # type: ignore[arg-type]
    )
    stored = await row(world)
    assert applied and stored["status"] == OutboxStatus.ACCEPTED
    assert stored["provider_message_id"] == "3EB0316FBDC6EC84F13164"  # what a quoted reply carries
    assert stored["provider_accepted_at"] == RELAY_T0 + timedelta(hours=3)  # the channel's clock


async def test_status_only_moves_forward(world: World, relay: RelayHandle) -> None:
    await make_message(world)
    await send(world, relay)
    accepted = result_from_gateway(
        "ACCEPTED", channel_message_id="msg_1", provider_message_id="3EB1"
    )
    assert accepted is not None
    await world.outbox.apply_channel_status("tenant-1", "msg_1", accepted)
    for late in ("QUEUED", "FAILED"):  # a late or contradictory report after the final one
        stale = result_from_gateway(late, channel_message_id="msg_1")
        assert stale is not None
        assert not await world.outbox.apply_channel_status("tenant-1", "msg_1", stale)
    assert (await row(world))["status"] == OutboxStatus.ACCEPTED
    queued = result_from_gateway("QUEUED", channel_message_id="msg_1")
    assert queued is not None and not await world.outbox.apply_channel_status(
        "tenant-9", "msg_1", queued
    )


async def test_a_lost_status_event_is_recovered_by_asking_the_gateway(
    world: World, relay: RelayHandle
) -> None:
    await make_message(world)
    await send(world, relay)
    relay.settle("msg_1", "ACCEPTED", provider_message_id="3EBPOLL")  # the event never reached us
    assert await reconciler(world, relay).run_once() == 1
    stored = await row(world)
    assert stored["status"] == OutboxStatus.ACCEPTED and stored["provider_message_id"] == "3EBPOLL"
    assert len(relay.sent) == 1


async def test_a_lost_response_is_resent_with_the_same_key_and_never_duplicates(
    world: World, relay: RelayHandle
) -> None:
    await make_message(world)
    key = (await row(world))["idempotency_key"]
    relay.state.fault = {"status_after_effect": 503}  # taken by the gateway, answer lost
    await send(world, relay)
    stored = await row(world)
    assert stored["status"] == OutboxStatus.UNKNOWN and stored["channel_message_id"] is None
    assert len(relay.sent) == 1

    relay.state.fault = None
    await reconciler(world, relay).run_once()  # no gateway id yet, inside the window: resend
    assert (await row(world))["status"] == OutboxStatus.PENDING
    await send(world, relay, "s2")
    stored = await row(world)
    assert stored["status"] == OutboxStatus.QUEUED and stored["channel_message_id"] == "msg_1"
    assert len(relay.sent) == 1 and relay.sent[0]["idempotency_key"] == key  # a REPLAY


async def test_a_send_the_gateway_never_got_is_created_by_the_resend(
    world: World, relay: RelayHandle
) -> None:
    await make_message(world)
    relay.state.fault = {"status": 503}  # refused before any effect, but the sender cannot know
    await send(world, relay)
    assert (await row(world))["status"] == OutboxStatus.UNKNOWN and relay.sent == []
    relay.state.fault = None
    await reconciler(world, relay).run_once()
    await send(world, relay, "s2")
    assert (await row(world))["status"] == OutboxStatus.QUEUED and len(relay.sent) == 1


async def test_an_unanswerable_status_query_proves_nothing_and_resends_nothing(
    world: World, relay: RelayHandle
) -> None:
    await make_message(world)
    await send(world, relay)
    relay.state.fault = {"status": 503}  # asking also fails
    await world.db.pool.execute(
        "UPDATE outbox_messages SET updated_at = updated_at - interval '1 hour'"
    )
    await reconciler(world, relay).run_once()
    stored = await row(world)
    assert stored["status"] == OutboxStatus.UNKNOWN and stored["last_error"] == "LOOKUP_FAILED"
    assert len(relay.sent) == 1


async def test_a_gateway_unknown_waits_for_a_decision_and_is_never_guessed(
    world: World, relay: RelayHandle
) -> None:
    await make_message(world)
    await send(world, relay)
    relay.settle("msg_1", "UNKNOWN")  # the gateway itself is unsure and holds the instance's queue
    await reconciler(world, relay).run_once()
    stored = await row(world)
    assert stored["status"] == OutboxStatus.UNKNOWN
    assert stored["last_error"] == "CHANNEL_UNKNOWN_NEEDS_DECISION"
    assert len(relay.sent) == 1  # nothing was resent behind the gateway's back


async def test_the_sender_does_not_resend_blindly_past_the_retry_horizon(
    world: World, relay: RelayHandle
) -> None:
    await make_message(world)
    with pytest.raises(SimulatedCrash):  # sent, the answer never recorded
        await world.outbox_worker(
            "s1", ChaosFaults("C08_during_outbox_send"), sender=gateway(relay)
        ).run_once()
    assert len(relay.sent) == 1

    tick(world, relay, TTL + timedelta(minutes=11))  # claim expired AND past the 10 min horizon
    assert (
        await world.outbox_worker(
            "s2", sender=gateway(relay), retry_horizon=POLICY.retry_horizon
        ).run_once()
        == 1
    )
    stored = await row(world)
    assert stored["status"] == OutboxStatus.UNKNOWN and stored["attempts"] == 2
    assert len(relay.state.requests) == 1  # the second attempt NEVER reached the wire

    await reconciler(world, relay).run_once()  # still inside the gateway's 24 h window: replay
    await send(world, relay, "s3", retry_horizon=POLICY.retry_horizon)
    stored = await row(world)
    assert stored["status"] == OutboxStatus.QUEUED and stored["channel_message_id"] == "msg_1"
    assert len(relay.sent) == 1  # the same message, found again by key


async def test_inside_the_horizon_a_stale_row_is_still_retried_with_the_same_key(
    world: World, relay: RelayHandle
) -> None:
    await make_message(world)
    with pytest.raises(SimulatedCrash):
        await world.outbox_worker(
            "s1", ChaosFaults("C08_during_outbox_send"), sender=gateway(relay)
        ).run_once()
    tick(world, relay, TTL + timedelta(seconds=1))  # well inside the horizon
    await send(world, relay, "s2", retry_horizon=POLICY.retry_horizon)
    assert (await row(world))["status"] == OutboxStatus.QUEUED and len(relay.sent) == 1


async def test_past_the_gateways_retention_no_resend_is_safe_and_nothing_is_resent(
    world: World, relay: RelayHandle
) -> None:
    await make_message(world)
    relay.state.fault = {"status_after_effect": 503}
    await send(world, relay)
    assert len(relay.sent) == 1 and (await row(world))["channel_message_id"] is None

    relay.state.fault = None
    tick(world, relay, RELAY_RETENTION + timedelta(minutes=10))  # the gateway forgot the key
    await reconciler(world, relay).run_once()
    stored = await row(world)
    assert stored["status"] == OutboxStatus.UNKNOWN
    assert stored["last_error"] == "UNPROVEN_PAST_RETENTION"
    assert len(relay.sent) == 1  # a resend would have created a second message


async def test_two_reconcilers_cannot_both_claim_the_same_row(
    world: World, relay: RelayHandle
) -> None:
    await make_message(world)
    relay.state.fault = {"status_after_effect": 503}
    await send(world, relay)
    first = await world.outbox.claim_unsettled("r1", 10, TTL, timedelta(0))
    second = await world.outbox.claim_unsettled("r2", 10, TTL, timedelta(0))
    assert len(first) == 1 and second == []  # the claim is exclusive


async def test_a_resend_authorised_inside_the_window_is_not_sent_after_it_closes(
    world: World, relay: RelayHandle
) -> None:  # the authorisation has a deadline of its own; the sender checks it AT SEND TIME
    await make_message(world)
    relay.state.fault = {"status_after_effect": 503}  # taken by the gateway, answer lost
    await send(world, relay)
    assert len(relay.sent) == 1

    relay.state.fault = None
    await reconciler(world, relay).run_once()  # inside the window: the resend is authorised
    stored = await row(world)
    assert stored["status"] == OutboxStatus.PENDING
    assert stored["resend_authorized_until"] is not None

    # the sender was down; it comes back after the gateway may have forgotten the key
    tick(world, relay, RELAY_RETENTION + timedelta(minutes=1))
    requests_before = len(relay.state.requests)
    assert await world.outbox_worker("late", sender=gateway(relay)).run_once() == 1
    assert len(relay.state.requests) == requests_before  # ZERO calls on the wire
    assert (await row(world))["status"] == OutboxStatus.UNKNOWN

    await reconciler(world, relay).run_once()  # now it is past retention: no resend is safe
    stored = await row(world)
    assert (
        stored["status"] == OutboxStatus.UNKNOWN
        and stored["last_error"] == "UNPROVEN_PAST_RETENTION"
    )
    assert len(relay.sent) == 1  # one message, never two


async def test_a_resend_authorised_inside_the_window_and_sent_inside_it_still_goes_out(
    world: World, relay: RelayHandle
) -> None:
    await make_message(world)
    relay.state.fault = {"status_after_effect": 503}
    await send(world, relay)
    relay.state.fault = None
    await reconciler(world, relay).run_once()
    tick(world, relay, timedelta(minutes=3))  # slow, but well inside the window
    await send(world, relay, "s2")
    assert (await row(world))["status"] == OutboxStatus.QUEUED and len(relay.sent) == 1
