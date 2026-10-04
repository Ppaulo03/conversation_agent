"""The whole edge, over real HTTP and PostgreSQL:

signed webhook -> durable inbox -> turn (with a transcribed voice note) -> durable outbox
-> gateway sender -> gateway
"""

from __future__ import annotations

import json
from datetime import UTC
from typing import Any

import httpx
import pytest

from conftest import RelayHandle
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.connections.static import StaticConnectionResolver
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.relayplane.asgi import webhook_app
from conversation_agent.adapters.relayplane.subscriptions import StaticSubscriptionResolver
from conversation_agent.adapters.relayplane.webhook import RelayPlaneWebhook
from conversation_agent.adapters.secrets.providers import InMemorySecretProvider
from conversation_agent.adapters.senders.relayplane import RelayPlaneSender
from conversation_agent.adapters.transcribers.fake import FakeTranscriber
from conversation_agent.core.models.connections import ResolvedConnection
from conversation_agent.ports.subscriptions import Subscription
from postgres.world import World
from relayplane_sim.main import inbound_event, sign

SECRET = "whsec-e2e"
SUB = Subscription(subscription_id="s1", tenant_id="tenant-1", channel_id="wa-1", secret_ref="wh")


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


async def deliver_webhook(world: World, payload: dict[str, Any]) -> httpx.Response:
    webhook = RelayPlaneWebhook(
        world.inbox,
        StaticSubscriptionResolver([SUB]),
        InMemorySecretProvider({("tenant-1", "wh"): SECRET}),
        world.clock,
    )
    body = json.dumps(payload).encode()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=webhook_app(webhook)), base_url="http://hook"
    ) as client:
        return await client.post(
            "/webhooks/relayplane/s1",
            content=body,
            headers={"X-Relay-Signature": sign(SECRET, body, int(world.clock.now().timestamp()))},
        )


def gateway(relay: RelayHandle) -> RelayPlaneSender:
    connection = ResolvedConnection(
        connection_id="relayplane",
        base_url=relay.base_url,
        allow_private_networks=True,
        tls_required=False,
    )
    return RelayPlaneSender(StaticConnectionResolver({"relayplane": connection}))


async def test_a_voice_note_goes_in_one_end_and_the_answer_comes_out_the_other(
    world: World, relay: RelayHandle
) -> None:
    media = [{"media_id": "voz-1", "kind": "audio", "mime_type": "audio/ogg", "size_bytes": 9000}]
    payload = inbound_event(
        "ev-voz",
        text=None,
        media=media,
        occurred_at=world.clock.now().astimezone(UTC),
    )
    assert (await deliver_webhook(world, payload)).status_code == 200  # persisted, then 2xx

    transcriber = FakeTranscriber({"voz-1": "queria saber se vocês abrem sábado"})
    llm = FakeLLM([text_response("Abrimos de segunda a sexta.")])
    run = await world.coordinator("w", llm, transcriber=transcriber).run_once()
    assert [r.status for r in run] == ["done"]
    seen = "".join(p.text for p in llm.requests[0].messages[-1].parts if hasattr(p, "text"))
    assert seen == "[voice message] queria saber se vocês abrem sábado"

    assert await world.outbox_worker("s", sender=gateway(relay)).run_once() == 1
    (delivered,) = relay.deliveries
    assert delivered["text"] == "Abrimos de segunda a sexta." and delivered["to"] == "contact-1"
    assert delivered["idempotency_key"]  # stable identity of the outbox row
    assert await world.db.pool.fetchval("SELECT status FROM outbox_messages") == "ACCEPTED"
