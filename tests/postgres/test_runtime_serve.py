"""Phase 13: the durable runtime assembled by `Runtime.build`, driven through the console channel.

A deployment no longer re-wires the stores and workers by hand: build the Runtime, receive events,
tick. The terminal channel proves what a confirmation needs from any channel (the sender reports
when it accepted a message; the inbound event names the message it answers).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from conftest import ApiHandle
from conversation_agent.adapters.clock import FixedClock, SystemClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response, tool_call_response
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.senders.console import ConsoleChannel
from conversation_agent.adapters.tools.http import HTTPToolProvider, local_dev_connection
from conversation_agent.app.runtime import Runtime
from conversation_agent.app.serve import Args, converse, parse_args
from conversation_agent.ports.clock import Clock
from support.builders import IDENTITY
from vertical_slice.definitions import CONNECTION
from vertical_slice.wiring import load_compiled_agent

NOW = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
BOOKING = {
    "service_id": "haircut",
    "start_at": "2026-10-06T10:00:00-03:00",
    "duration_minutes": 30,
}


@pytest.fixture
def clock() -> Clock:
    """The real clock: coordination time is the database's, so a frozen application clock would
    put every message in the 'future' of the workers (the reliability tests fix BOTH)."""
    return SystemClock("America/Sao_Paulo")


def build(
    db: PostgresDatabase, clock: Clock, api: ApiHandle, llm: FakeLLM, out: list[str]
) -> tuple[Runtime, ConsoleChannel]:
    channel = ConsoleChannel(IDENTITY, clock, write=out.append)
    runtime = Runtime.build(
        db=db,
        compiled=load_compiled_agent(),
        llm=llm,
        providers={
            "http": HTTPToolProvider.static({CONNECTION: local_dev_connection(api.base_url)})
        },
        sender=channel,
        clock=clock,
    )
    return runtime, channel


async def test_a_reservation_is_proposed_confirmed_and_executed_through_the_runtime(
    db: PostgresDatabase, clock: Clock, api: ApiHandle
) -> None:
    out: list[str] = []
    llm = FakeLLM(
        [
            tool_call_response("scheduling__create", BOOKING),
            text_response("Posso reservar o horário."),
            text_response("Reservado! Seu horário está confirmado."),
        ]
    )
    runtime, channel = build(db, clock, api, llm, out)

    await runtime.receive(channel.inbound("quero terça às 10h"))
    await runtime.drain()
    assert out[-1].startswith("bot> Posso reservar o horário.") and "SIM" in out[-1]
    assert api.state.bookings == {}  # proposed only: nothing executed yet

    # the answer comes at the very same instant (the fixed clock): the time evidence is
    # ambiguous, but the event names the message it answers, which is proof enough
    await runtime.receive(channel.inbound("sim"))
    await runtime.drain()
    assert len(api.state.bookings) == 1
    assert out[-1] == "bot> Reservado! Seu horário está confirmado."


async def test_without_the_message_it_answers_a_yes_is_not_taken_as_a_confirmation(
    db: PostgresDatabase, clock: Clock, api: ApiHandle
) -> None:
    out: list[str] = []
    llm = FakeLLM(
        [tool_call_response("scheduling__create", BOOKING), text_response("Posso reservar.")]
    )
    runtime, channel = build(db, clock, api, llm, out)
    await runtime.receive(channel.inbound("quero terça às 10h"))
    await runtime.drain()

    bare = channel.inbound("sim").model_copy(
        update={"reply_to_provider_message_id": None, "provider_occurred_at": None}
    )
    await runtime.receive(bare)
    await runtime.drain()
    assert api.state.bookings == {}  # no evidence it answers the prompt: nothing runs
    assert "Não entendi" in out[-1]  # and it asks again (INV-022)


async def test_the_same_event_delivered_twice_is_one_turn(
    db: PostgresDatabase, clock: Clock, api: ApiHandle
) -> None:
    out: list[str] = []
    runtime, channel = build(db, clock, api, FakeLLM([text_response("Olá!")]), out)
    event = channel.inbound("oi")
    assert await runtime.receive(event) is True
    assert await runtime.receive(event) is False
    await runtime.drain()
    assert out == ["bot> Olá!"]


class AheadClock:
    """An application clock an hour AHEAD of the database's (a badly synchronised worker)."""

    def now(self) -> datetime:
        return datetime.now(UTC) + timedelta(hours=1)


async def test_a_worker_whose_clock_runs_ahead_does_not_delay_its_own_messages(
    db: PostgresDatabase, api: ApiHandle
) -> None:  # INV-032: delivery is due by the coordination clock, not by the writer's own
    out: list[str] = []
    runtime, channel = build(db, AheadClock(), api, FakeLLM([text_response("Olá!")]), out)
    await runtime.receive(channel.inbound("oi"))
    await runtime.drain()
    assert out == ["bot> Olá!"]


async def test_a_stage_that_fails_does_not_stop_the_others(
    db: PostgresDatabase, clock: Clock, api: ApiHandle, caplog: pytest.LogCaptureFixture
) -> None:
    out: list[str] = []
    runtime, channel = build(db, clock, api, FakeLLM([text_response("Olá!")]), out)

    async def broken(limit: int = 0) -> int:
        raise RuntimeError("scheduler down")

    runtime.scheduler_worker.run_once = broken  # type: ignore[method-assign]
    await runtime.receive(channel.inbound("oi"))
    await runtime.drain()
    assert len(out) == 1  # the turn ran and the reply was delivered anyway
    assert "runtime stage failed" in caplog.text


async def test_converse_reads_lines_until_the_input_ends_and_delivers_the_last_reply(
    db: PostgresDatabase, clock: Clock, api: ApiHandle
) -> None:
    out: list[str] = []
    runtime, channel = build(db, clock, api, FakeLLM([text_response("Oi, tudo bem?")]), out)
    lines = iter(["", "olá"])

    def read_line() -> str:
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    await converse(runtime, channel, read_line=read_line, write=out.append, poll_interval=0.01)
    assert out[-1] == "bot> Oi, tudo bem?"


# --- the terminal channel and the command line (no database) ---


def test_the_console_shows_a_message_once_per_idempotency_key() -> None:
    import asyncio

    clock = FixedClock(NOW)

    from conversation_agent.core.models.runtime import OutboundMessage

    out: list[str] = []
    channel = ConsoleChannel(IDENTITY, clock, write=out.append)
    message = OutboundMessage(
        outbox_id="o1",
        tenant_id=IDENTITY.tenant_id,
        conversation_id=IDENTITY.conversation_id,
        channel_id=IDENTITY.channel_id,
        contact_id=IDENTITY.contact_id,
        turn_id="t1",
        message_index=0,
        text="oi",
        idempotency_key="k1",
    )
    first = asyncio.run(channel.send(message))
    again = asyncio.run(channel.send(message))  # a retry after a crash
    assert first == again and out == ["bot> oi"]
    assert first.provider_accepted_at == clock.now()  # the channel says when it accepted it
    assert channel.inbound("sim").reply_to_provider_message_id == first.provider_message_id


def test_inbound_events_have_unique_ids_and_the_channels_own_time() -> None:
    clock = FixedClock(NOW)
    channel = ConsoleChannel(IDENTITY, clock)
    a, b = channel.inbound("um"), channel.inbound("dois")
    assert a.event_id != b.event_id and a.provider_occurred_at == clock.now()
    assert a.reply_to_provider_message_id is None  # nothing was shown yet


def test_the_command_line_is_parsed_strictly() -> None:
    args = parse_args(
        ["agent.yaml", "--http", "api=http://localhost:8001", "--tenant", "t1", "--packs", "p"]
    )
    assert args == Args("agent.yaml", {"api": "http://localhost:8001"}, "p", "t1", "terminal")
    for bad in (
        [],
        ["a.yaml", "b.yaml"],
        ["a.yaml", "--http", "no-equals"],
        ["a.yaml", "--http"],
        ["a.yaml", "--surprise", "1"],
    ):
        with pytest.raises(ValueError):
            parse_args(bad)
