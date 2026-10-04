"""Proactive messages (DoD: timers/proactive survive restart; HUMAN stays silent; the channel
policy decides; outbound only from the outbox)."""

from __future__ import annotations

from datetime import time, timedelta

import pytest

from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.policies.service_window import ServiceWindowPolicy
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.models.conversation import ConversationIdentity
from conversation_agent.engine.proactive import proactive_timer
from postgres.world import World, event
from support.builders import IDENTITY

POLICY = ServiceWindowPolicy(window=timedelta(hours=24))


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


async def user_wrote(world: World) -> None:
    """The contact wrote and got an answer: the service window is open from now."""
    await world.inbox.insert_if_absent(event("u1", "oi", clock=world.clock))
    run = await world.coordinator(
        "c", FakeLLM([text_response("Olá!")]), channel_policy=POLICY
    ).run_once()
    assert [r.status for r in run] == ["done"]


async def fire(world: World, owner: str = "p") -> int:
    return await world.proactive_worker(owner).run_once()


async def deliver(world: World, owner: str = "c2", policy: object = POLICY) -> list[str]:
    run = await world.coordinator(owner, FakeLLM([]), channel_policy=policy).run_once()
    return [r.status for r in run]


async def timer(
    world: World,
    hours: float,
    text: str = "Lembrete: sua consulta é amanhã.",
    reason: str = "reminder",
) -> None:
    await world.scheduler.schedule(
        proactive_timer(
            IDENTITY, text=text, due_at=world.clock.now() + timedelta(hours=hours), reason=reason
        )
    )


def later(world: World, hours: float) -> None:
    world.clock.set(world.clock.now() + timedelta(hours=hours))


async def outbox_texts(world: World) -> list[str]:
    return [
        r["text"]
        for r in await world.db.pool.fetch(
            "SELECT text FROM outbox_messages ORDER BY created_at, message_index"
        )
    ]


async def test_a_timer_survives_a_restart_and_sends_exactly_one_message(
    db: PostgresDatabase, clock: FixedClock
) -> None:
    before = World(db, clock)
    await user_wrote(before)
    await timer(before, hours=2)
    first_outbox = len(await outbox_texts(before))

    after = World(db, clock)  # a brand-new process: nothing in memory survived
    later(after, 2.5)
    assert await fire(after) == 1
    assert await deliver(after) == ["done"]
    texts = await outbox_texts(after)
    assert texts[first_outbox:] == ["Lembrete: sua consulta é amanhã."]
    assert await after.outbox_worker("s").run_once() == 2  # the hello and the reminder
    assert [m.text for m in after.sender.delivered][-1].startswith("Lembrete")


async def test_a_timer_fired_twice_is_one_event_and_one_message(world: World) -> None:
    await user_wrote(world)
    await timer(world, hours=1)
    later(world, 1.5)
    await world.proactive_worker("p1").run_once()
    # the claim is abandoned before it was marked done: another worker fires the SAME timer
    await world.db.pool.execute(
        "UPDATE scheduled_events SET status='PENDING', claim_expires_at=NULL"
    )
    await world.proactive_worker("p2").run_once()
    assert await world.count("inbox_events", "kind='system'") == 1
    await deliver(world)
    assert len([t for t in await outbox_texts(world) if t.startswith("Lembrete")]) == 1


async def test_the_channel_policy_blocks_a_message_outside_the_service_window(
    world: World,
) -> None:
    await user_wrote(world)
    await timer(world, hours=30)
    later(world, 31)
    await fire(world)
    assert await deliver(world) == ["done"]
    assert [t for t in await outbox_texts(world) if t.startswith("Lembrete")] == []
    assert await world.count("inbox_events", "status='CONSUMED' AND kind='system'") == 1
    decision = await world.db.pool.fetchval(
        "SELECT payload FROM turn_journal WHERE step_type='PROACTIVE_DECISION'"
    )
    assert decision["send"] is False and decision["reason"] == "OUTSIDE_SERVICE_WINDOW"


async def test_without_a_channel_policy_nothing_proactive_is_ever_sent(world: World) -> None:
    await user_wrote(world)
    await timer(world, hours=1)
    later(world, 2)
    await fire(world)
    assert await deliver(world, policy=None) == ["done"]
    assert [t for t in await outbox_texts(world) if t.startswith("Lembrete")] == []  # fail closed


async def test_a_human_owned_conversation_stays_silent_for_timers(world: World) -> None:
    await user_wrote(world)
    await world.db.pool.execute("UPDATE conversation_states SET ownership='HUMAN'")
    history_before = await world.db.pool.fetchval("SELECT state_json FROM conversation_states")
    await timer(world, hours=1)
    later(world, 2)
    await fire(world)
    llm = FakeLLM([])
    run = await world.coordinator("c3", llm, channel_policy=POLICY).run_once()
    assert [r.status for r in run] == ["done"] and llm.calls == 0
    assert [t for t in await outbox_texts(world) if t.startswith("Lembrete")] == []
    assert (
        await world.db.pool.fetchval("SELECT state_json FROM conversation_states") == history_before
    )


async def test_our_own_message_does_not_extend_the_service_window(world: World) -> None:
    await user_wrote(world)
    await timer(world, hours=20, text="Lembrete 1", reason="r1")
    later(world, 21)
    await fire(world)
    await deliver(world)
    assert "Lembrete 1" in await outbox_texts(world)  # inside the window: sent
    await timer(world, hours=5, text="Lembrete 2", reason="r2")  # 26 h after the contact wrote
    later(world, 6)
    await fire(world)
    await deliver(world, "c4")
    assert "Lembrete 2" not in await outbox_texts(world)  # the window was NOT renewed by us


async def test_a_runtime_event_never_shares_a_turn_with_what_the_contact_wrote(
    world: World,
) -> None:
    await user_wrote(world)
    await timer(world, hours=1)
    later(world, 2)
    await fire(world)
    await world.inbox.insert_if_absent(event("u2", "e a quarta?", clock=world.clock))
    llm = FakeLLM([text_response("Quarta tem horário.")])
    run = await world.coordinator("c5", llm, channel_policy=POLICY).run_once()
    assert [r.status for r in run] == ["done"] and llm.calls == 1  # the model saw only the user
    assert await world.count("turns", "status='COMPLETED'") == 3  # hello, user, timer: separate
    assert "Lembrete: sua consulta é amanhã." in await outbox_texts(world)


async def test_quiet_hours_are_enforced_by_the_policy(clock: FixedClock) -> None:
    identity: ConversationIdentity = IDENTITY
    policy = ServiceWindowPolicy(timedelta(hours=24), quiet_hours=(time(22, 0), time(8, 0)))
    now = clock.now()  # 08:00 local: just outside the quiet range
    wrote = now - timedelta(hours=1)
    assert (await policy.evaluate(identity, now=now, last_contact_event_at=wrote, kind="p")).allowed
    night = now.replace(hour=23)
    blocked = await policy.evaluate(identity, now=night, last_contact_event_at=night, kind="p")
    assert not blocked.allowed and blocked.reason == "QUIET_HOURS"
    never = await policy.evaluate(identity, now=now, last_contact_event_at=None, kind="p")
    assert never.reason == "NEVER_CONTACTED"
