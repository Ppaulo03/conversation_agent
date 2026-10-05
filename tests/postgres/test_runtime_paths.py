"""Phase 14: paths through the assembled Runtime that the POC could not cover.

- the whole reservation with NO model call (a Flow proposes, rules read the "yes", a template says
  the result);
- a write whose answer was lost, recovered by the Runtime's own reconciler by LOOKUP (the API
  counts every POST, so a re-send could not hide behind the API's idempotency);
- several runtimes of one scope sharing the work without answering anyone twice.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from conftest import ApiHandle
from conversation_agent.adapters.clock import FixedClock, SystemClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response, tool_call_response
from conversation_agent.adapters.manifest.yaml_loader import load_manifest_file
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.senders.console import ConsoleChannel
from conversation_agent.adapters.tools.http import HTTPToolProvider, local_dev_connection
from conversation_agent.app.runtime import Runtime
from conversation_agent.core.compiler import CompiledAgent, compile_manifest
from conversation_agent.core.models.conversation import ConversationIdentity
from conversation_agent.core.models.runtime import OutboundMessage, SendResult
from conversation_agent.ports.clock import Clock
from vertical_slice.definitions import CONNECTION
from vertical_slice.wiring import MANIFEST_PATH

T0 = datetime(2026, 10, 5, 8, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))
TEMPLATE = "Pronto! Reserva {booking_id} confirmada para {start_at}."


def compiled(template: str | None = TEMPLATE) -> CompiledAgent:
    raw = load_manifest_file(MANIFEST_PATH)
    for capability in raw["capabilities"]:
        if capability["name"] == "scheduling.create" and template is not None:
            capability["executed_template"] = template
    return compile_manifest(raw)


def identity(name: str) -> ConversationIdentity:
    return ConversationIdentity(
        tenant_id="tenant-1",
        channel_id="console",
        conversation_id=f"conv-{name}",
        session_id=f"sess-{name}",
        contact_id=f"contact-{name}",
    )


def runtime_for(
    db: PostgresDatabase,
    api: ApiHandle,
    llm: FakeLLM,
    channel: ConsoleChannel,
    clock: Clock,
    agent: CompiledAgent | None = None,
    owner: str | None = None,
) -> Runtime:
    return Runtime.build(
        db=db,
        compiled=agent or compiled(),
        llm=llm,
        providers={
            "http": HTTPToolProvider.static({CONNECTION: local_dev_connection(api.base_url)})
        },
        sender=channel,
        clock=clock,
        owner=owner,
    )


async def test_a_whole_reservation_with_no_model_call(db: PostgresDatabase, api: ApiHandle) -> None:
    out: list[str] = []
    clock = FixedClock(T0)
    channel = ConsoleChannel(identity("a"), clock, write=out.append)
    llm = FakeLLM([])  # any model call fails the test
    runtime = runtime_for(db, api, llm, channel, clock)

    await runtime.receive(channel.inbound("Quero marcar um corte amanhã às 10h"))
    await runtime.drain()
    assert "Agendar haircut em ter 06/10 às 10:00" in out[-1] and "SIM" in out[-1]
    assert api.state.bookings == {}

    await runtime.receive(channel.inbound("sim"))
    await runtime.drain()
    (booking_id,) = api.state.bookings
    assert out[-1] == f"bot> Pronto! Reserva {booking_id} confirmada para ter 06/10 às 10:00."
    assert llm.calls == 0


async def test_a_lost_answer_is_recovered_by_the_runtime_by_lookup_not_by_resending(
    db: PostgresDatabase, api: ApiHandle
) -> None:
    out: list[str] = []
    clock = SystemClock("America/Sao_Paulo")
    channel = ConsoleChannel(identity("b"), clock, write=out.append)
    llm = FakeLLM(
        [
            tool_call_response(
                "scheduling__create",
                {
                    "service_id": "haircut",
                    "start_at": "2026-10-06T10:00:00-03:00",
                    "duration_minutes": 30,
                },
            ),
            text_response("Posso reservar."),
        ]
    )
    runtime = runtime_for(db, api, llm, channel, clock)
    await runtime.receive(channel.inbound("quero terça às 10h"))
    await runtime.drain()

    api.state.fault = {"status_after_effect": 503}  # the booking IS made, the answer is lost
    await runtime.receive(channel.inbound("sim"))
    await runtime.coordinator.run_once()  # only the turn: it waits, the outcome is not known
    api.state.fault = None  # the API is back before the reconciler looks the booking up
    await runtime.drain()

    assert len(api.state.bookings) == 1  # one booking
    posts = [r for r in api.requests if r["method"] == "POST"]
    assert len(posts) == 1  # and one POST: it was FOUND by lookup, never sent again
    (booking_id,) = api.state.bookings
    assert out[-1] == f"bot> Pronto! Reserva {booking_id} confirmada para ter 06/10 às 10:00."


class SharedChannel:
    """One 'channel' for every conversation: records who was told what, deduping like a provider."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self._seen: dict[str, SendResult] = {}

    async def send(self, message: OutboundMessage) -> SendResult:
        known = self._seen.get(message.idempotency_key)
        if known is not None:
            return known
        result = SendResult(status="ACCEPTED", provider_message_id=f"m{len(self.sent)}")  # type: ignore[arg-type]
        self._seen[message.idempotency_key] = result
        self.sent.append((message.conversation_id, message.text))
        return result


@pytest.mark.parametrize("workers", [2, 3])
async def test_several_runtimes_of_one_scope_answer_every_conversation_exactly_once(
    db: PostgresDatabase, api: ApiHandle, workers: int
) -> None:
    clock = SystemClock("America/Sao_Paulo")
    shared = SharedChannel()
    names = [f"c{i}" for i in range(8)]
    runtimes = [
        Runtime.build(
            db=db,
            compiled=compiled(),
            llm=FakeLLM([text_response("Olá!")] * len(names)),
            providers={
                "http": HTTPToolProvider.static({CONNECTION: local_dev_connection(api.base_url)})
            },
            sender=shared,  # type: ignore[arg-type]
            clock=clock,
            owner=f"w{i}",
        )
        for i in range(workers)
    ]
    for name in names:
        await runtimes[0].receive(ConsoleChannel(identity(name), clock).inbound("oi"))

    await asyncio.gather(*(r.drain() for r in runtimes))
    told = sorted(conversation for conversation, _ in shared.sent)
    assert told == sorted(f"conv-{n}" for n in names)  # each exactly once, nobody twice
    assert await db.pool.fetchval("SELECT count(*) FROM turns WHERE status <> 'COMPLETED'") == 0


class SlowGateway:
    """A channel that takes the message ('queued', here is my id) and only later says what became
    of it; sending it again would be a second delivery, so the answer is looked up."""

    def __init__(self) -> None:
        self.sends = 0
        self.lookups = 0

    async def send(self, message: OutboundMessage) -> SendResult:
        self.sends += 1
        return SendResult(status="QUEUED", channel_message_id=f"gw-{message.outbox_id[:8]}")  # type: ignore[arg-type]

    async def lookup(self, message: OutboundMessage) -> SendResult:
        self.lookups += 1
        return SendResult(
            status="ACCEPTED",  # type: ignore[arg-type]
            provider_message_id="prov-1",
            provider_accepted_at=datetime.now(ZoneInfo("UTC")),
            channel_message_id=message.channel_message_id,
        )


async def test_a_send_the_channel_left_unsettled_is_reconciled_by_lookup_not_re_sent(
    db: PostgresDatabase, api: ApiHandle
) -> None:
    from datetime import timedelta

    from conversation_agent.core.models.delivery import DeliveryPolicy

    gateway = SlowGateway()
    runtime = Runtime.build(
        db=db,
        compiled=compiled(),
        llm=FakeLLM([text_response("Olá!")]),
        providers={
            "http": HTTPToolProvider.static({CONNECTION: local_dev_connection(api.base_url)})
        },
        sender=gateway,  # type: ignore[arg-type]
        clock=SystemClock("America/Sao_Paulo"),
        delivery_policy=DeliveryPolicy(
            idempotency_retention=timedelta(hours=1), retry_horizon=timedelta(minutes=10)
        ),
        outbox_poll_after=timedelta(0),
    )
    channel = ConsoleChannel(identity("d"), SystemClock("America/Sao_Paulo"))
    await runtime.receive(channel.inbound("oi"))
    await runtime.drain()

    status = await db.pool.fetchval("SELECT status FROM outbox_messages")
    assert status == "ACCEPTED"  # settled by the Runtime's own outbox reconciler
    assert gateway.sends == 1 and gateway.lookups >= 1  # asked the channel, never sent twice


def test_a_delivery_policy_needs_a_sender_that_can_look_a_message_up() -> None:
    from datetime import timedelta

    from conversation_agent.core.models.delivery import DeliveryPolicy

    with pytest.raises(ValueError, match="look a message up"):
        Runtime.build(
            db=None,  # type: ignore[arg-type]
            compiled=compiled(),
            llm=FakeLLM([]),
            providers={},
            sender=SharedChannel(),  # type: ignore[arg-type]
            delivery_policy=DeliveryPolicy(
                idempotency_retention=timedelta(hours=1), retry_horizon=timedelta(minutes=10)
            ),
        )


# --- the recovery window of a dead worker is the deployment's to choose ---


async def test_a_dead_workers_conversation_is_recovered_after_the_configured_lease_ttl(
    db: PostgresDatabase, api: ApiHandle
) -> None:
    from datetime import timedelta

    from conversation_agent.adapters.postgres.coordination import CoordinationTime
    from conversation_agent.adapters.postgres.lease import PostgresLeaseStore
    from conversation_agent.core.models.runtime import ConversationKey

    clock = SystemClock("America/Sao_Paulo")
    out: list[str] = []
    channel = ConsoleChannel(identity("e"), clock, write=out.append)
    runtime = Runtime.build(
        db=db,
        compiled=compiled(),
        llm=FakeLLM([text_response("Voltei!")]),
        providers={
            "http": HTTPToolProvider.static({CONNECTION: local_dev_connection(api.base_url)})
        },
        sender=channel,
        clock=clock,
        lease_ttl=timedelta(seconds=3),
    )
    # a worker took the conversation and died: its lease is never renewed
    key = ConversationKey(tenant_id="tenant-1", conversation_id="conv-e")
    await runtime.receive(channel.inbound("oi"))
    dead = PostgresLeaseStore(db, clock, CoordinationTime(db))
    assert await dead.acquire(key, "dead-worker", timedelta(seconds=3)) is not None

    await runtime.coordinator.run_once()
    assert out == []  # still held: nobody may take it yet

    await asyncio.sleep(3.5)  # the short TTL, not the 30 s default
    await runtime.drain()
    assert out == ["bot> Voltei!"]


def test_the_lease_ttl_must_leave_room_for_the_heartbeat() -> None:
    from datetime import timedelta

    def build(**kw: object) -> Runtime:
        return Runtime.build(
            db=None,  # type: ignore[arg-type]
            compiled=compiled(),
            llm=FakeLLM([]),
            providers={},
            sender=SharedChannel(),  # type: ignore[arg-type]
            **kw,  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="more than twice"):
        build(lease_ttl=timedelta(seconds=5), heartbeat_interval_seconds=3.0)
    with pytest.raises(ValueError, match="more than twice"):
        build(lease_ttl=timedelta(seconds=0))
    build(lease_ttl=timedelta(seconds=3))  # the heartbeat follows the TTL: 1 s
    build(lease_ttl=timedelta(seconds=30), heartbeat_interval_seconds=10.0)


# --- messages sent in pieces ---


def with_window(db, api, llm, out, **kw):  # type: ignore[no-untyped-def]
    clock = SystemClock("America/Sao_Paulo")
    channel = ConsoleChannel(identity("w"), clock, write=out.append)
    return (
        Runtime.build(
            db=db,
            compiled=compiled(),
            llm=llm,
            providers={
                "http": HTTPToolProvider.static({CONNECTION: local_dev_connection(api.base_url)})
            },
            sender=channel,
            clock=clock,
            **kw,
        ),
        channel,
    )


async def test_a_turn_waits_until_the_contact_has_been_quiet_for_the_debounce(
    db: PostgresDatabase, api: ApiHandle
) -> None:
    from datetime import timedelta

    out: list[str] = []
    llm = FakeLLM([text_response("Entendi tudo.")])
    runtime, channel = with_window(db, api, llm, out, debounce=timedelta(seconds=1.5))

    await runtime.receive(channel.inbound("quero reservar"))
    await asyncio.sleep(0.8)
    await runtime.receive(channel.inbound("futsal"))
    await asyncio.sleep(0.8)
    await runtime.receive(
        channel.inbound("amanhã às 18h")
    )  # still typing: each piece restarts the wait
    await runtime.coordinator.run_once()
    assert out == [] and llm.calls == 0  # nothing yet: 0.0 s since the last piece

    await runtime.drain()  # waits out the window, then ONE turn takes all three pieces
    assert out == ["bot> Entendi tudo."] and llm.calls == 1
    seen = str(llm.requests[0].messages)
    assert "quero reservar" in seen and "futsal" in seen and "amanhã às 18h" in seen


async def test_the_wait_never_exceeds_the_maximum_even_if_the_contact_keeps_typing(
    db: PostgresDatabase, api: ApiHandle
) -> None:
    from datetime import timedelta

    out: list[str] = []
    llm = FakeLLM([text_response("Pronto.")])
    runtime, channel = with_window(
        db,
        api,
        llm,
        out,
        debounce=timedelta(seconds=2),
        debounce_max_wait=timedelta(seconds=3),  # never more than 3 s since the FIRST piece
    )
    await runtime.receive(channel.inbound("um"))  # t = 0
    await asyncio.sleep(1.0)
    await runtime.receive(channel.inbound("dois"))  # t = 1: the 2 s window restarts
    await asyncio.sleep(1.0)
    await runtime.receive(channel.inbound("tres"))  # t = 2: ...and again (it would end at t = 4)
    await asyncio.sleep(0.5)
    await runtime.coordinator.run_once()  # t = 2.5: still inside both the window and the cap
    assert llm.calls == 0 and out == []

    await asyncio.sleep(0.8)  # t = 3.3: the cap (3 s since the first piece) opens the turn
    await runtime.coordinator.run_once()
    await runtime.outbox_worker.run_once()
    assert out == ["bot> Pronto."] and llm.calls == 1


async def test_without_a_debounce_a_turn_opens_at_once_as_before(
    db: PostgresDatabase, api: ApiHandle
) -> None:
    out: list[str] = []
    runtime, channel = with_window(db, api, FakeLLM([text_response("Oi!")]), out)
    await runtime.receive(channel.inbound("oi"))
    await runtime.coordinator.run_once()  # no waiting at all
    await runtime.outbox_worker.run_once()
    assert out == ["bot> Oi!"]


def test_the_debounce_cannot_be_longer_than_its_maximum() -> None:
    from datetime import timedelta

    with pytest.raises(ValueError, match="debounce"):
        Runtime.build(
            db=None,  # type: ignore[arg-type]
            compiled=compiled(),
            llm=FakeLLM([]),
            providers={},
            sender=SharedChannel(),  # type: ignore[arg-type]
            debounce=timedelta(seconds=20),
            debounce_max_wait=timedelta(seconds=5),
        )


async def test_restart_on_new_message_is_off_by_default_and_can_be_turned_on(
    db: PostgresDatabase, api: ApiHandle
) -> None:
    from datetime import timedelta

    from conversation_agent.adapters.postgres.coordination import CoordinationTime
    from conversation_agent.adapters.postgres.lease import PostgresLeaseStore
    from conversation_agent.core.models.runtime import ConversationKey

    key = ConversationKey(tenant_id="tenant-1", conversation_id="conv-w")
    clock = SystemClock("America/Sao_Paulo")
    leases = PostgresLeaseStore(db, clock, CoordinationTime(db))

    async def flagged(**kw: object) -> bool:
        runtime, channel = with_window(db, api, FakeLLM([]), [], **kw)
        await runtime.receive(channel.inbound("primeira"))  # creates the conversation
        await db.pool.execute("UPDATE inbox_events SET status = 'CONSUMED'")
        assert await leases.acquire(key, "busy-worker", timedelta(seconds=30)) is not None
        await runtime.receive(channel.inbound("segunda"))  # arrives while a turn is "in progress"
        value = bool(await db.pool.fetchval("SELECT cancel_requested FROM conversation_states"))
        await db.pool.execute("TRUNCATE conversation_states, inbox_events")
        return value

    assert await flagged() is False  # queued: the turn in progress finishes first
    assert (
        await flagged(restart_on_new_message=True) is True
    )  # asked to stop at the next safe point


def test_with_a_delivery_policy_the_retry_horizon_has_one_source() -> None:
    from datetime import timedelta

    from conversation_agent.core.models.delivery import DeliveryPolicy

    policy = DeliveryPolicy(
        idempotency_retention=timedelta(hours=24), retry_horizon=timedelta(minutes=10)
    )

    def build(**kw: object) -> Runtime:
        return Runtime.build(
            db=None,  # type: ignore[arg-type]
            compiled=compiled(),
            llm=FakeLLM([]),
            providers={},
            sender=SlowGateway(),  # type: ignore[arg-type]
            delivery_policy=policy,
            **kw,  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="retry horizon comes from it"):
        build(outbox_retry_horizon=timedelta(hours=48))  # would let the worker retry blind for 48 h
    build(outbox_retry_horizon=timedelta(minutes=10))  # the same value is harmless
    build()
