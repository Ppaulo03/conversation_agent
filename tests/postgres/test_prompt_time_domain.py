"""INV-026: `provider_accepted_at` is the CHANNEL's acceptance time or nothing; it is never the
local/database clock. Eligibility compares provider-domain timestamps only; without a comparable
one the answer is AMBIGUOUS and the runtime asks again."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import ApiHandle
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.senders.fake import FakeMessageSender
from postgres.test_protected_actions import answer, deliver_prompt, pending, posts, propose
from postgres.world import World


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


async def accepted_at(world: World):  # type: ignore[no-untyped-def]
    return await world.db.pool.fetchval("SELECT provider_accepted_at FROM outbox_messages")


async def test_a_channel_that_gives_no_timestamp_leaves_accepted_at_null_not_local_time(
    world: World, api: ApiHandle
) -> None:
    world.sender = FakeMessageSender()  # ACCEPTED, but no provider-domain time
    await propose(world, api)
    await deliver_prompt(world)
    row = await world.db.pool.fetchrow("SELECT status, provider_accepted_at FROM outbox_messages")
    assert row is not None and row["status"] == "ACCEPTED" and row["provider_accepted_at"] is None


async def test_without_comparable_time_a_bare_yes_is_ambiguous_but_reply_to_still_works(
    world: World, api: ApiHandle
) -> None:
    world.sender = FakeMessageSender()
    await propose(world, api)
    await deliver_prompt(world)
    await answer(world, api, "sim", FakeLLM([]))
    assert posts(api) == []  # no comparable evidence -> nothing executes
    assert (await pending(world))["confirmation_attempts"] == 1  # it asked again

    await deliver_prompt(world)
    provider_id = await world.db.pool.fetchval(
        "SELECT provider_message_id FROM outbox_messages "
        "ORDER BY created_at DESC, message_index DESC"
    )
    await answer(
        world, api, "sim", FakeLLM([text_response("Agendado!")]), reply_to=provider_id, after_s=5
    )
    assert len(posts(api)) == 1  # an explicit reply-to is evidence that needs no clock


async def test_accepted_at_is_the_channel_time_even_when_it_differs_from_the_local_clock(
    world: World, api: ApiHandle
) -> None:
    world.channel_skew = timedelta(seconds=-8)  # the channel's clock runs 8 s behind ours
    await propose(world, api)
    local_now = world.clock.now()
    await deliver_prompt(world)
    assert await accepted_at(world) == local_now - timedelta(seconds=8)  # the channel's time


async def test_a_legitimate_reply_is_eligible_when_the_channel_clock_runs_behind(
    world: World, api: ApiHandle
) -> None:
    """With local stamping this reply (channel time = accepted + 5 s) would look like it came
    3 s BEFORE the prompt and be refused; compared in the channel's own domain it is eligible."""
    world.channel_skew = timedelta(seconds=-8)
    await propose(world, api)
    await deliver_prompt(world)  # channel says accepted at (local - 8 s)
    await answer(
        world, api, "sim", FakeLLM([text_response("Agendado!")]), after_s=0, skew_s=-3
    )  # channel time of the reply = local - 3 s = accepted + 5 s
    assert len(posts(api)) == 1 and (await pending(world))["status"] == "CONFIRMED"
