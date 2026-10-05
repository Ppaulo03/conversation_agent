"""Phase 14.1 (audit): the messages of one conversation leave in the order they were written.

A conversation's outbound messages get a sequence number from the conversation itself, in the
transaction that writes them. A worker may only claim a message whose predecessors are no longer
waiting to be sent, so two workers (and `split_replies`) can never put part 2 ahead of part 1, and a
worker with a skewed clock cannot reorder turns.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.postgres.conversations import ensure_conversation
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.models.conversation import ConversationIdentity
from conversation_agent.core.models.runtime import (
    ConversationKey,
    OutboundMessage,
    OutboxStatus,
    SendResult,
)
from postgres.world import TTL, World


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


def identity(name: str) -> ConversationIdentity:
    return ConversationIdentity(
        tenant_id="tenant-1",
        channel_id="chan",
        conversation_id=f"conv-{name}",
        session_id=f"sess-{name}",
        contact_id=f"contact-{name}",
    )


async def write(world: World, name: str, texts: list[str], *, turn: str = "t1") -> None:
    """One turn's messages, written in ONE transaction by the lease holder (like a real turn)."""
    who = identity(name)
    await ensure_conversation(world.db.pool, who, world.clock.now())
    key = ConversationKey(tenant_id=who.tenant_id, conversation_id=who.conversation_id)
    lease = await world.leases.acquire(key, "writer", TTL)
    assert lease is not None
    async with world.uows.begin(lease.fence) as uow:
        for index, text in enumerate(texts):
            await uow.outbox.add(
                OutboundMessage(
                    outbox_id=f"{name}-{turn}-{index}",
                    tenant_id=who.tenant_id,
                    conversation_id=who.conversation_id,
                    channel_id=who.channel_id,
                    contact_id=who.contact_id,
                    turn_id=turn,
                    message_index=index,
                    text=text,
                    idempotency_key=f"{name}-{turn}-{index}",
                )
            )
        await uow.commit()
    await world.leases.release(lease)


async def claim(world: World, owner: str, **kw: object) -> list[str]:
    claimed = await world.outbox.claim_ready(owner, 20, TTL, **kw)  # type: ignore[arg-type]
    return [m.text for m in claimed]


async def settle(world: World, status: OutboxStatus = OutboxStatus.ACCEPTED) -> None:
    """The channel took what is SENDING (a worker finished its send)."""
    rows = await world.db.pool.fetch("SELECT outbox_id FROM outbox_messages WHERE status='SENDING'")
    for r in rows:
        await world.db.pool.execute(
            "UPDATE outbox_messages SET status=$2 WHERE outbox_id=$1", r["outbox_id"], status.value
        )


async def test_two_workers_cannot_send_part_2_before_part_1(world: World) -> None:
    await write(world, "a", ["Encontrei estes horários.", "Confirma a reserva?"])

    assert await claim(world, "worker-a") == ["Encontrei estes horários."]
    assert (
        await claim(world, "worker-b") == []
    )  # part 2 may not jump over part 1 (SKIP LOCKED once did)

    await settle(world)  # part 1 reached the channel
    assert await claim(world, "worker-b") == ["Confirma a reserva?"]


async def test_two_turns_keep_their_order_even_when_a_workers_clock_ran_behind(
    world: World,
) -> None:
    world.clock.set(world.clock.now() + timedelta(seconds=10))
    await write(world, "a", ["primeiro turno"], turn="t1")
    world.clock.set(world.clock.now() - timedelta(seconds=10))  # takeover by a worker 10 s behind
    await write(world, "a", ["segundo turno"], turn="t2")
    created = await world.db.pool.fetch(
        "SELECT text, created_at FROM outbox_messages ORDER BY text"
    )
    assert created[1]["created_at"] < created[0]["created_at"]  # the clocks DO disagree...

    world.clock.set(world.clock.now() + timedelta(seconds=20))  # both are due now
    assert await claim(world, "worker-a") == ["primeiro turno"]  # ...and the order still holds
    await settle(world)
    assert await claim(world, "worker-a") == ["segundo turno"]


async def test_different_conversations_do_not_wait_for_each_other(world: World) -> None:
    await write(world, "a", ["a1", "a2"])
    await write(world, "b", ["b1", "b2"])
    assert sorted(await claim(world, "w")) == ["a1", "b1"]  # one per conversation per claim


@pytest.mark.parametrize(
    "status",
    [OutboxStatus.QUEUED, OutboxStatus.ACCEPTED, OutboxStatus.FAILED, OutboxStatus.SUPERSEDED],
)
async def test_a_predecessor_the_channel_has_dealt_with_never_holds_the_next_back(
    world: World, status: OutboxStatus
) -> None:
    await write(world, "a", ["um", "dois"])
    await world.db.pool.execute(
        "UPDATE outbox_messages SET status=$1 WHERE text='um'", status.value
    )
    assert await claim(world, "w") == ["dois"]


@pytest.mark.parametrize("status", ["SENDING", "RECONCILING"])
async def test_a_predecessor_being_sent_or_reconciled_holds_the_next_back(
    world: World, status: str
) -> None:
    await write(world, "a", ["um", "dois"])
    await world.db.pool.execute(
        "UPDATE outbox_messages SET status=$1, claim_expires_at = now() + interval '1 hour' "
        "WHERE text='um'",
        status,
    )
    assert await claim(world, "w") == []


async def test_a_predecessor_waiting_to_be_sent_goes_first_and_alone(world: World) -> None:
    await write(world, "a", ["um", "dois"])
    assert await claim(world, "w") == ["um"]  # "dois" is not claimable while "um" is PENDING


async def test_a_predecessor_waiting_for_its_retry_time_holds_the_next_back(world: World) -> None:
    await write(world, "a", ["um", "dois"])
    await world.db.pool.execute(
        "UPDATE outbox_messages SET available_at = $1 WHERE text='um'",
        world.clock.now() + timedelta(minutes=1),  # a failed send backing off
    )
    assert await claim(world, "w") == []  # not "dois" instead: order beats availability


async def test_an_unknown_outcome_holds_the_next_back_only_inside_the_idempotency_window(
    world: World,
) -> None:
    await write(world, "a", ["um", "dois"])
    await world.db.pool.execute(
        "UPDATE outbox_messages SET status='UNKNOWN', first_sent_at = $1 WHERE text='um'",
        world.clock.now() - timedelta(minutes=10),
    )
    # inside the window the first may still be resent with its key: sending the second now could
    # put it ahead
    assert await claim(world, "w", unknown_blocks_for=timedelta(hours=1)) == []
    # past it nothing proves the first can ever be resent: it must not silence the conversation
    assert await claim(world, "w", unknown_blocks_for=timedelta(minutes=5)) == ["dois"]


async def test_one_run_sends_all_the_parts_of_a_reply_back_to_back_and_in_order(
    world: World,
) -> None:
    await write(world, "a", ["parte 1", "parte 2", "parte 3"])
    await world.outbox_worker("w").run_once()
    assert [m.text for m in world.sender.delivered] == ["parte 1", "parte 2", "parte 3"]


async def test_workers_racing_over_many_conversations_still_deliver_each_in_order(
    world: World,
) -> None:
    for name in "abcd":
        await write(world, name, [f"{name}{i}" for i in range(1, 5)])
    original = world.sender.send

    async def slow(message: OutboundMessage) -> SendResult:
        await asyncio.sleep(0.01)  # overlap the workers: a send takes time
        return await original(message)

    world.sender.send = slow  # type: ignore[method-assign]
    workers = [world.outbox_worker(f"w{i}") for i in range(3)]
    for _ in range(10):
        await asyncio.gather(*(w.run_once() for w in workers))

    delivered = [m.text for m in world.sender.delivered]
    assert len(delivered) == 16
    for name in "abcd":
        assert [t for t in delivered if t.startswith(name)] == [f"{name}{i}" for i in range(1, 5)]
