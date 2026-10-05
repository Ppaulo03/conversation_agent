"""The whole edge, over real HTTP and PostgreSQL:

signed webhook -> durable inbox -> turn (with a transcribed voice note) -> durable outbox
-> gateway sender -> gateway -> status event back through the webhook
"""

from __future__ import annotations

import json
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
from relayplane_sim.main import envelope, message_received, webhook_headers
from vertical_slice.definitions import build_agent

SECRET = "whsec_e2e"
SUB = Subscription(subscription_id="s1", tenant_id="tenant-1", secret_ref="wh")


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


async def deliver_webhook(world: World, payload: dict[str, Any]) -> httpx.Response:
    webhook = RelayPlaneWebhook(
        world.inbox,
        world.outbox,
        StaticSubscriptionResolver([SUB]),
        InMemorySecretProvider({("tenant-1", "wh"): SECRET}),
        world.clock,
    )
    body = json.dumps(payload).encode()
    headers = webhook_headers(SECRET, body, int(world.clock.now().timestamp()), payload["event_id"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=webhook_app(webhook)), base_url="http://hook"
    ) as client:
        return await client.post("/webhooks/relayplane/s1", content=body, headers=headers)


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
    media = {
        "media_id": "voz-1",
        "status": "READY",
        "kind": "audio",
        "mime_type": "audio/ogg",
        "size": 9000,
    }
    payload = message_received("ev-voz", text=None, media=media, timestamp=world.clock.now())
    assert (await deliver_webhook(world, payload)).status_code == 200  # persisted, then 2xx

    transcriber = FakeTranscriber({"voz-1": "queria saber se vocês abrem sábado"})
    llm = FakeLLM([text_response("Abrimos de segunda a sexta.")])
    run = await world.coordinator(
        "w", llm, transcriber=transcriber, agent=build_agent(transcription="on")
    ).run_once()
    assert [r.status for r in run] == ["done"]
    seen = "".join(p.text for p in llm.requests[0].messages[-1].parts if hasattr(p, "text"))
    assert seen == "[voice message] queria saber se vocês abrem sábado"

    assert await world.outbox_worker("s", sender=gateway(relay)).run_once() == 1
    (delivered,) = relay.sent
    assert delivered["payload"] == {"text": "Abrimos de segunda a sexta."}
    assert delivered["to"] == "5511999990000" and delivered["instance_id"] == "inst_1"
    assert delivered["idempotency_key"]  # stable identity of the outbox row
    assert await world.db.pool.fetchval("SELECT status FROM outbox_messages") == "QUEUED"

    relay.settle("msg_1", "ACCEPTED", provider_message_id="3EBOUT1")  # the provider took it...
    status = {"message_id": "msg_1", "status": "ACCEPTED", "provider_message_id": "3EBOUT1"}
    event = envelope("message.outbound_status", status, event_id="ev-status")
    assert (await deliver_webhook(world, event)).json() == {"status": "applied"}  # ...we learn it
    assert await world.db.pool.fetchval("SELECT status FROM outbox_messages") == "ACCEPTED"
    assert await world.db.pool.fetchval("SELECT provider_message_id FROM outbox_messages") == (
        "3EBOUT1"
    )
