"""The runtime's own log lines (Phase 11): what an operator gets when something goes wrong, from
the real components, with the context bound where the work happens and no personal data."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from conftest import ApiHandle
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.observability.logs import configure_logging
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.relayplane.asgi import webhook_app
from conversation_agent.adapters.relayplane.subscriptions import StaticSubscriptionResolver
from conversation_agent.adapters.relayplane.webhook import RelayPlaneWebhook
from conversation_agent.adapters.secrets.providers import InMemorySecretProvider
from conversation_agent.core.errors import LLMProviderError
from conversation_agent.core.models.runtime import OutboxStatus, SendResult
from conversation_agent.core.observability import conversation_ref
from conversation_agent.ports.subscriptions import Subscription
from postgres.test_protected_actions import answer, coord, deliver_prompt
from postgres.world import KEY, World, event
from relayplane_sim.main import message_received, webhook_headers
from support.builders import IDENTITY

TENANT = IDENTITY.tenant_id
CONVERSATION = IDENTITY.conversation_id


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    out = io.StringIO()
    handler = configure_logging(logging.DEBUG, stream=out)
    yield out
    logging.getLogger("conversation_agent").removeHandler(handler)
    logging.getLogger("conversation_agent").propagate = True


def records(stream: io.StringIO, event_name: str | None = None) -> list[dict[str, Any]]:
    parsed = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    return [r for r in parsed if event_name is None or r["event"] == event_name]


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


async def test_a_storage_failure_at_the_edge_is_logged_with_its_event_and_tenant(
    world: World, stream: io.StringIO
) -> None:
    class Down:
        async def insert_if_absent(self, event: Any) -> bool:
            raise ConnectionError("database down for ana@example.com")

    secret = "whsec_logs"
    webhook = RelayPlaneWebhook(
        Down(),  # type: ignore[arg-type]
        world.outbox,
        StaticSubscriptionResolver(
            [Subscription(subscription_id="s", tenant_id=TENANT, secret_ref="wh")]
        ),
        InMemorySecretProvider({(TENANT, "wh"): secret}),
        world.clock,
    )
    payload = message_received(
        "ev-77", sender="5511999990000", timestamp=world.clock.now(), instance_id="inst_1"
    )
    body = json.dumps(payload).encode()
    headers = webhook_headers(secret, body, int(world.clock.now().timestamp()), "ev-77")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=webhook_app(webhook)), base_url="http://hook"
    ) as client:
        response = await client.post("/webhooks/relayplane/s", content=body, headers=headers)
    assert response.status_code == 503
    (line,) = records(stream, "webhook.not_applied")
    assert line["level"] == "error" and line["exc_type"] == "ConnectionError"
    assert (line["tenant_id"], line["event_id"], line["component"]) == (TENANT, "ev-77", "webhook")
    assert line["channel_id"] == "inst_1" and line["event_type"] == "message.received"
    assert "ana@example.com" not in stream.getvalue()
    assert "5511999990000" not in stream.getvalue()  # the contact's number never reaches a log


async def test_a_turn_that_keeps_failing_is_logged_with_its_whole_context(
    world: World, api: ApiHandle, stream: io.StringIO
) -> None:
    await world.inbox.insert_if_absent(event("e-1", "oi", clock=world.clock))
    llm = FakeLLM([LLMProviderError("provider down for ana@example.com")] * 3)
    coordinator = world.coordinator("w", llm, max_turn_attempts=1)
    await coordinator.process_conversation(KEY)
    (line,) = records(stream, "turn.failed_permanently")
    assert (
        line["level"] == "error" and line["alert"] is True and line["error"] == "LLMProviderError"
    )
    assert line["tenant_id"] == TENANT and line["component"] == "coordinator"
    assert line["conversation_ref"] == conversation_ref(TENANT, CONVERSATION)
    assert line["turn_id"] and line["channel_id"] == IDENTITY.channel_id
    assert line["agent_id"] and line["agent_version"]  # which agent version was running
    assert CONVERSATION not in stream.getvalue() and "ana@example.com" not in stream.getvalue()


async def test_a_reply_the_channel_did_not_confirm_is_logged_per_outbox_row(
    world: World, stream: io.StringIO
) -> None:
    class Flaky:
        async def send(self, message: Any) -> SendResult:
            return SendResult(status=OutboxStatus.UNKNOWN)

    await world.inbox.insert_if_absent(event("e-1", "oi", clock=world.clock))
    await world.coordinator("c", FakeLLM([text_response("Olá!")])).run_once()
    await world.outbox_worker("s", sender=Flaky()).run_once()
    (line,) = records(stream, "outbox.send_not_confirmed")
    assert line["level"] == "warning" and line["status"] == "UNKNOWN" and line["attempts"] == 1
    assert line["tenant_id"] == TENANT and line["component"] == "outbox_worker"
    assert line["outbox_id"] == await world.db.pool.fetchval(
        "SELECT outbox_id FROM outbox_messages"
    )


async def test_a_reconciled_write_logs_its_invocation_and_what_it_found(
    world: World, api: ApiHandle, stream: io.StringIO
) -> None:
    await world.inbox.insert_if_absent(
        event("e-1", "Quero marcar um corte amanhã às 10h", clock=world.clock)
    )
    await coord(world, api, "w", FakeLLM([]), flows=True).process_conversation(KEY)
    await deliver_prompt(world)
    api.state.fault = {"status_after_effect": 503}  # the booking exists, the answer was lost
    await answer(world, api, "sim", FakeLLM([text_response("...")]), flows=True)
    api.state.fault = None
    (final,) = await world.reconciler(
        "r", providers={"http": world.http_provider(api.base_url)}
    ).run_once()
    (line,) = records(stream, "reconciliation.resolved")
    assert line["invocation_id"] == final.invocation_id and line["status"] == "RECONCILED"
    assert line["tenant_id"] == TENANT and line["component"] == "reconciliation"
    # the tool call itself carried the invocation in its context too
    assert any(r.get("invocation_id") == final.invocation_id for r in records(stream))
