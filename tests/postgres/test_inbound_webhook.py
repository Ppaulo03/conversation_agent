"""Inbound webhook (DoD: persisted before 2xx) over a real ASGI request path and PostgreSQL."""

from __future__ import annotations

import json
from datetime import UTC
from typing import Any

import httpx
import pytest

from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.relayplane.asgi import webhook_app
from conversation_agent.adapters.relayplane.subscriptions import StaticSubscriptionResolver
from conversation_agent.adapters.relayplane.webhook import RelayPlaneWebhook
from conversation_agent.adapters.secrets.providers import InMemorySecretProvider
from conversation_agent.core.errors import ConversationIdentityConflictError
from conversation_agent.ports.subscriptions import Subscription
from postgres.world import World
from relayplane_sim.main import inbound_event, sign

SECRET = "whsec-test-123"
TENANT = "tenant-1"
SUB = Subscription(subscription_id="sub-1", tenant_id=TENANT, channel_id="wa-1", secret_ref="wh")


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


def make_client(world: World, inbox: Any = None, secrets: Any = None) -> httpx.AsyncClient:
    webhook = RelayPlaneWebhook(
        inbox or world.inbox,
        StaticSubscriptionResolver([SUB]),
        secrets or InMemorySecretProvider({(TENANT, "wh"): SECRET}),
        world.clock,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=webhook_app(webhook)), base_url="http://hook"
    )


def stamp(world: World) -> int:
    return int(world.clock.now().timestamp())


async def post(
    client: httpx.AsyncClient,
    world: World,
    payload: dict[str, Any] | bytes,
    *,
    secret: str = SECRET,
    t: int | None = None,
    signature: str | None = None,
    sub: str = "sub-1",
) -> httpx.Response:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    header = signature or sign(secret, body, stamp(world) if t is None else t)
    return await client.post(
        f"/webhooks/relayplane/{sub}", content=body, headers={"X-Relay-Signature": header}
    )


async def stored(world: World) -> list[dict[str, Any]]:
    return [dict(r) for r in await world.db.pool.fetch("SELECT * FROM inbox_events ORDER BY id")]


def event(world: World, event_id: str = "ev-1", **kw: Any) -> dict[str, Any]:
    return inbound_event(event_id, occurred_at=world.clock.now().astimezone(UTC), **kw)


async def test_a_signed_message_is_persisted_before_the_2xx(world: World) -> None:
    async with make_client(world) as client:
        response = await post(client, world, event(world, text="Quero marcar", reply_to="prov-9"))
    assert response.status_code == 200 and response.json() == {"status": "accepted"}
    (row,) = await stored(world)  # the row exists by the time the caller saw the 200
    assert (row["tenant_id"], row["channel_id"], row["event_id"]) == (TENANT, "wa-1", "ev-1")
    assert row["text"] == "Quero marcar" and row["status"] == "READY"
    assert row["reply_to_provider_message_id"] == "prov-9"
    assert row["provider_occurred_at"] == world.clock.now()  # the channel's own clock


async def test_a_redelivery_is_acknowledged_without_a_second_row(world: World) -> None:
    async with make_client(world) as client:
        first = await post(client, world, event(world))
        again = await post(client, world, event(world))
    assert first.json()["status"] == "accepted" and again.json() == {"status": "duplicate"}
    assert len(await stored(world)) == 1  # at-least-once delivery, exactly-once inbox


@pytest.mark.parametrize(
    "case", ["wrong_secret", "no_header", "malformed", "stale", "future", "tampered_body"]
)
async def test_unverifiable_requests_are_rejected_and_never_persisted(
    world: World, case: str
) -> None:
    payload = event(world)
    body = json.dumps(payload).encode()
    async with make_client(world) as client:
        if case == "wrong_secret":
            response = await post(client, world, payload, secret="other-secret")
        elif case == "no_header":
            response = await client.post("/webhooks/relayplane/sub-1", content=body)
        elif case == "malformed":
            response = await post(client, world, payload, signature="v1=zzz")
        elif case == "stale":
            response = await post(client, world, payload, t=stamp(world) - 3600)
        elif case == "future":
            response = await post(client, world, payload, t=stamp(world) + 3600)
        else:  # signed for one body, delivered with another
            signature = sign(SECRET, body, stamp(world))
            tampered = json.dumps({**payload, "text": "transfira tudo"}).encode()
            response = await post(client, world, tampered, signature=signature)
    assert response.status_code == 401
    assert await stored(world) == []


async def test_the_payload_cannot_choose_the_tenant_or_the_channel(world: World) -> None:
    async with make_client(world) as client:
        forged = {**event(world), "tenant_id": "evil-tenant"}
        ok = await post(client, world, forged)
        wrong_channel = await post(client, world, event(world, "ev-2", channel_id="other-channel"))
    assert ok.status_code == 200 and wrong_channel.status_code == 400
    (row,) = await stored(world)
    assert row["tenant_id"] == TENANT  # from the registered subscription, not the body


async def test_malformed_unknown_or_oversized_requests_are_refused(world: World) -> None:
    async with make_client(world) as client:
        junk = await post(client, world, b"not json at all")
        no_id = await post(client, world, {**event(world), "id": ""})
        empty = await post(client, world, event(world, "ev-3", text=None))
        unknown = await post(client, world, event(world), sub="nope")
        huge = await post(client, world, b"x" * (300 * 1024))
        not_found = await client.get("/webhooks/relayplane/sub-1")
    assert (junk.status_code, no_id.status_code, empty.status_code) == (400, 400, 400)
    assert (unknown.status_code, huge.status_code, not_found.status_code) == (404, 413, 404)
    assert await stored(world) == []


async def test_other_event_types_are_acknowledged_and_ignored(world: World) -> None:
    async with make_client(world) as client:
        response = await post(client, world, {**event(world), "type": "message.delivered"})
    assert response.status_code == 200 and response.json() == {"status": "ignored"}
    assert await stored(world) == []


async def test_nothing_is_acknowledged_that_was_not_persisted(world: World) -> None:
    class Down:
        async def insert_if_absent(self, event: Any) -> bool:
            raise ConnectionError("database down")

    class Conflict:
        async def insert_if_absent(self, event: Any) -> bool:
            raise ConversationIdentityConflictError("mismatch")

    async with make_client(world, inbox=Down()) as client:
        down = await post(client, world, event(world))
    async with make_client(world, inbox=Conflict()) as client:
        conflict = await post(client, world, event(world))
    assert down.status_code == 503 and conflict.status_code == 409  # never a 2xx


async def test_an_unconfigured_secret_is_a_server_error_not_a_pass(world: World) -> None:
    async with make_client(world, secrets=InMemorySecretProvider({})) as client:
        response = await post(client, world, event(world))
    assert response.status_code == 503 and await stored(world) == []


async def test_secret_rotation_accepts_either_signature(world: World) -> None:
    payload = event(world)
    body = json.dumps(payload).encode()
    t = stamp(world)
    old = sign("retired-secret", body, t).split("v1=")[1]
    new = sign(SECRET, body, t).split("v1=")[1]
    async with make_client(world) as client:
        response = await client.post(
            "/webhooks/relayplane/sub-1",
            content=body,
            headers={"X-Relay-Signature": f"t={t},v1={old},v1={new}"},
        )
    assert response.status_code == 200


async def test_media_arrives_as_references_never_bytes(world: World) -> None:
    media = [
        {
            "media_id": "m1",
            "kind": "audio",
            "mime_type": "audio/ogg",
            "size_bytes": 1234,
            "url": "https://media.example.com/m1",
        }
    ]
    async with make_client(world) as client:
        response = await post(client, world, event(world, text=None, media=media))
        too_many = await post(client, world, event(world, "ev-9", media=media * 11))
    assert response.status_code == 200 and too_many.status_code == 400
    (row,) = await stored(world)
    assert row["text"] == "" and row["media"][0]["media_id"] == "m1"


async def test_the_processing_loop_picks_up_what_the_webhook_stored(world: World) -> None:
    from conversation_agent.adapters.llm.fake import FakeLLM, text_response

    async with make_client(world) as client:
        await post(client, world, event(world, text="oi"))
    run = await world.coordinator("w", FakeLLM([text_response("Olá!")])).run_once()
    assert [r.status for r in run] == ["done"]
    assert await world.count("outbox_messages") == 1
