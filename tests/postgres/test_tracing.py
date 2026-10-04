"""Tracing (Phase 11): one trace id from the inbound event to the reply it caused, spans with
parents and timings, and a tracer that can never change what the work does."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import httpx
import pytest

from conftest import ApiHandle
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.observability.logs import configure_logging
from conversation_agent.adapters.observability.tracing import InMemoryTracer, LogTracer
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.relayplane.asgi import webhook_app
from conversation_agent.adapters.relayplane.subscriptions import StaticSubscriptionResolver
from conversation_agent.adapters.relayplane.webhook import RelayPlaneWebhook
from conversation_agent.adapters.secrets.providers import InMemorySecretProvider
from conversation_agent.core.observability import bind, current
from conversation_agent.core.tracing import (
    configure_tracer,
    new_trace_id,
    span,
    trace_id_for,
)
from conversation_agent.ports.subscriptions import Subscription
from postgres.world import World, event
from relayplane_sim.main import message_received, webhook_headers

SECRET = "whsec_trace"
SUB = Subscription(subscription_id="s", tenant_id="tenant-1", secret_ref="wh")


@pytest.fixture
def tracer() -> Iterator[InMemoryTracer]:
    t = InMemoryTracer()
    configure_tracer(t)
    yield t
    configure_tracer(None)


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


# --- the spans themselves ---


def test_a_span_has_a_trace_a_parent_a_duration_and_a_status(tracer: InMemoryTracer) -> None:
    with bind(trace_id="trace-1"), span("outer", step=1):
        with span("inner"):
            pass
        with pytest.raises(ValueError), span("failing"):
            raise ValueError("boom for ana@example.com")
    inner, failing, outer = tracer.spans
    assert {s.trace_id for s in tracer.spans} == {"trace-1"}
    assert inner.parent_id == outer.span_id == failing.parent_id and outer.parent_id is None
    assert (failing.status, failing.error_type) == ("error", "ValueError")
    assert "ana@example.com" not in repr(failing)  # only the type, never the message
    assert outer.status == "ok" and outer.duration_ms >= 0 and outer.attributes == {"step": 1}


def test_a_span_mints_a_trace_when_there_is_none_and_children_share_it(
    tracer: InMemoryTracer,
) -> None:
    assert "trace_id" not in current()
    with span("root"):
        assert current()["trace_id"]  # bound for everything inside (logs, usage records, ...)
        with span("child"):
            pass
    child, root = tracer.spans
    assert child.trace_id == root.trace_id and len(root.trace_id) == 32
    assert "trace_id" not in current()  # and gone again afterwards


def test_attributes_are_scalars_scrubbed_and_capped(tracer: InMemoryTracer) -> None:
    with span("x", n=3, ok=True, note="call +55 11 99999-0000 or ana@example.com", big="y" * 999,
              obj=object()):  # fmt: skip
        pass
    attrs = tracer.spans[0].attributes
    assert attrs["n"] == 3 and attrs["ok"] is True
    assert "99999-0000" not in attrs["note"] and "ana@example.com" not in attrs["note"]
    assert len(attrs["big"]) == 200 and "object" in attrs["obj"]


def test_without_a_tracer_spans_cost_nothing_and_a_failing_tracer_changes_nothing() -> None:
    configure_tracer(None)
    with span("quiet"):
        pass

    class Broken:
        def on_end(self, span: object) -> None:
            raise RuntimeError("exporter down")

    configure_tracer(Broken())
    try:
        with span("work"):
            result = 42
    finally:
        configure_tracer(None)
    assert result == 42


def test_the_legacy_trace_id_is_derived_from_the_turn_and_a_bound_one_wins() -> None:
    assert trace_id_for("turn-9") == "trace-turn-9"
    with bind(trace_id="minted"):
        assert trace_id_for("turn-9") == "minted"
    assert len(new_trace_id()) == 32 and new_trace_id() != new_trace_id()


def test_the_log_tracer_writes_one_json_line_per_span_with_the_context() -> None:
    out = io.StringIO()
    handler = configure_logging(logging.INFO, stream=out)
    configure_tracer(LogTracer())
    try:
        with (
            bind(tenant_id="t1", turn_id="turn-1", trace_id="tr-1"),
            span("llm.call", purpose="agent"),
        ):
            pass
    finally:
        configure_tracer(None)
        logging.getLogger("conversation_agent").removeHandler(handler)
        logging.getLogger("conversation_agent").propagate = True
    (line,) = [json.loads(x) for x in out.getvalue().splitlines()]
    assert line["event"] == "span" and line["span"] == "llm.call" and line["status"] == "ok"
    assert (line["trace_id"], line["tenant_id"], line["turn_id"]) == ("tr-1", "t1", "turn-1")
    assert (
        line["attr_purpose"] == "agent" and line["duration_ms"] >= 0 and line["parent_id"] is None
    )


# --- one trace across the whole runtime ---


async def receive(world: World, text: str, event_id: str = "ev-1") -> httpx.Response:
    webhook = RelayPlaneWebhook(
        world.inbox, world.outbox, StaticSubscriptionResolver([SUB]),
        InMemorySecretProvider({("tenant-1", "wh"): SECRET}), world.clock,
    )  # fmt: skip
    payload = message_received(event_id, text=text, timestamp=world.clock.now())
    body = json.dumps(payload).encode()
    headers = webhook_headers(SECRET, body, int(world.clock.now().timestamp()), event_id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=webhook_app(webhook)), base_url="http://hook"
    ) as client:
        return await client.post("/webhooks/relayplane/s", content=body, headers=headers)


async def test_a_message_keeps_one_trace_from_the_webhook_to_the_reply(
    world: World, api: ApiHandle, tracer: InMemoryTracer
) -> None:
    assert (await receive(world, "Quero marcar um corte amanhã às 10h")).status_code == 200
    stored = await world.db.pool.fetchval("SELECT trace_id FROM inbox_events")
    assert stored and len(stored) == 32  # minted at the edge, persisted with the event

    await world.coordinator(
        "w", FakeLLM([]), flows=True, providers={"http": world.http_provider(api.base_url)}
    ).run_once()
    await world.outbox_worker("sender").run_once()

    assert await world.db.pool.fetchval("SELECT trace_id FROM outbox_messages") == stored
    context = await world.db.pool.fetchval(
        "SELECT context_json FROM tool_invocations WHERE capability = 'scheduling.availability'"
    )
    assert context["trace_id"] == stored  # the tool call carries it to the external system
    trace = tracer.trace(stored)
    names = [s.name for s in trace]
    assert {"webhook.receive", "turn", "tool.call", "outbox.send"} <= set(names)
    assert {s.trace_id for s in tracer.spans} == {stored}  # nothing strayed into another trace
    turn = next(s for s in trace if s.name == "turn")
    assert next(s for s in trace if s.name == "tool.call").parent_id == turn.span_id
    assert next(s for s in trace if s.name == "webhook.receive").parent_id is None


async def test_an_llm_call_is_a_child_span_of_its_turn_and_named_by_purpose(
    world: World, tracer: InMemoryTracer
) -> None:
    await receive(world, "oi")
    await world.coordinator("w", FakeLLM([text_response("Olá!")])).run_once()
    trace_id = await world.db.pool.fetchval("SELECT trace_id FROM inbox_events")
    turn = next(s for s in tracer.trace(trace_id) if s.name == "turn")
    llm = next(s for s in tracer.trace(trace_id) if s.name == "llm.call")
    assert llm.parent_id == turn.span_id and llm.attributes == {"purpose": "agent"}
    assert llm.duration_ms <= turn.duration_ms


async def test_events_from_before_tracing_keep_working_with_the_per_turn_fallback(
    world: World, tracer: InMemoryTracer
) -> None:
    await world.inbox.insert_if_absent(event("e-1", "oi", clock=world.clock))  # no trace id
    await world.coordinator("w", FakeLLM([text_response("Olá!")])).run_once()
    turn_id = await world.db.pool.fetchval("SELECT turn_id FROM turns")
    assert (
        await world.db.pool.fetchval("SELECT trace_id FROM outbox_messages") == f"trace-{turn_id}"
    )
    assert {s.trace_id for s in tracer.spans} == {f"trace-{turn_id}"}


async def test_a_reply_and_its_send_share_the_trace_even_across_processes(
    world: World, tracer: InMemoryTracer
) -> None:
    await receive(world, "oi")
    await world.coordinator("w", FakeLLM([text_response("Olá!")])).run_once()
    tracer.spans.clear()  # a different process later picks the row up: only the ROW carries the trace
    configure_tracer(tracer)
    await world.outbox_worker("another-process").run_once()
    (send,) = tracer.spans
    assert send.name == "outbox.send"
    assert send.trace_id == await world.db.pool.fetchval("SELECT trace_id FROM inbox_events")
