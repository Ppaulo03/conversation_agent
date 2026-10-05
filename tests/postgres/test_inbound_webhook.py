"""Inbound webhook (DoD: persisted before 2xx) over a real ASGI request path and PostgreSQL,
speaking the gateway's published envelope and signature (docs/RELAYPLANE_CONTRACT.md)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.relayplane.asgi import webhook_app
from conversation_agent.adapters.relayplane.subscriptions import StaticSubscriptionResolver
from conversation_agent.adapters.relayplane.webhook import RelayPlaneWebhook
from conversation_agent.adapters.secrets.providers import InMemorySecretProvider
from conversation_agent.core.errors import ConversationIdentityConflictError
from conversation_agent.core.models.runtime import OutboxStatus
from conversation_agent.ports.subscriptions import Subscription
from postgres.world import World, event
from relayplane_sim.main import envelope, message_received, webhook_headers

SECRET = "whsec_test_123"
TENANT = "tenant-1"
SUB = Subscription(
    subscription_id="sub-1",
    tenant_id=TENANT,
    secret_ref="wh",
    relay_tenant_id="tenant_relay",
    instance_ids=("inst_1",),
)


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


def make_client(
    world: World, inbox: Any = None, secrets: Any = None, sub: Subscription = SUB
) -> httpx.AsyncClient:
    webhook = RelayPlaneWebhook(
        inbox or world.inbox,
        world.outbox,
        StaticSubscriptionResolver([sub]),
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
    headers: dict[str, str] | None = None,
    sub: str = "sub-1",
) -> httpx.Response:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    event_id = payload.get("event_id", "x") if isinstance(payload, dict) else "x"
    sent = headers or webhook_headers(secret, body, stamp(world) if t is None else t, event_id)
    return await client.post(f"/webhooks/relayplane/{sub}", content=body, headers=sent)


async def stored(world: World) -> list[dict[str, Any]]:
    return [dict(r) for r in await world.db.pool.fetch("SELECT * FROM inbox_events ORDER BY id")]


def received(world: World, event_id: str = "ev-1", **kw: Any) -> dict[str, Any]:
    return message_received(event_id, timestamp=world.clock.now(), **kw)


async def test_a_signed_message_is_persisted_before_the_2xx(world: World) -> None:
    async with make_client(world) as client:
        response = await post(
            client, world, received(world, text="Quero marcar", reply_to="3EBprev", sequence=7)
        )
    assert response.status_code == 200 and response.json() == {"status": "accepted"}
    (row,) = await stored(world)  # the row exists by the time the caller saw the 200
    assert (row["tenant_id"], row["channel_id"], row["event_id"]) == (TENANT, "inst_1", "ev-1")
    assert row["conversation_id"] == "inst_1:5511999990000"  # instance + the contact's number
    assert row["text"] == "Quero marcar" and row["status"] == "READY"
    assert row["reply_to_provider_message_id"] == "3EBprev"
    assert row["provider_message_id"] == "3EBev-1" and row["source_sequence"] == 7
    assert row["provider_occurred_at"] == world.clock.now()  # the channel's own clock


async def test_a_redelivery_is_acknowledged_without_a_second_row(world: World) -> None:
    async with make_client(world) as client:
        first = await post(client, world, received(world))
        again = await post(client, world, received(world))
    assert first.json()["status"] == "accepted" and again.json() == {"status": "duplicate"}
    assert len(await stored(world)) == 1  # at-least-once delivery, exactly-once inbox


@pytest.mark.parametrize(
    "case", ["wrong_secret", "no_header", "malformed", "stale", "future", "tampered_body"]
)
async def test_unverifiable_requests_are_rejected_and_never_persisted(
    world: World, case: str
) -> None:
    payload = received(world)
    body = json.dumps(payload).encode()
    async with make_client(world) as client:
        if case == "wrong_secret":
            response = await post(client, world, payload, secret="other-secret")
        elif case == "no_header":
            response = await client.post("/webhooks/relayplane/sub-1", content=body)
        elif case == "malformed":
            headers = webhook_headers(SECRET, body, stamp(world), "ev-1")
            response = await post(
                client, world, payload, headers={**headers, "X-RelayPlane-Signature": "v1=zzz"}
            )
        elif case == "stale":
            response = await post(client, world, payload, t=stamp(world) - 3600)
        elif case == "future":
            response = await post(client, world, payload, t=stamp(world) + 3600)
        else:  # signed for one body, delivered with another
            headers = webhook_headers(SECRET, body, stamp(world), "ev-1")
            tampered = json.dumps({**payload, "event_id": "other"}).encode()
            response = await post(client, world, tampered, headers=headers)
    assert response.status_code == 401
    assert await stored(world) == []


async def test_the_payload_cannot_choose_the_tenant(world: World) -> None:
    async with make_client(world) as client:
        forged = received(world, tenant_id="evil-tenant")  # not the gateway tenant registered
        refused = await post(client, world, forged)
        elsewhere = await post(client, world, received(world, "ev-2", instance_id="inst_other"))
    assert refused.status_code == 400
    assert elsewhere.status_code == 200 and elsewhere.json()["reason"] == "instance_not_subscribed"
    assert await stored(world) == []


async def test_without_a_registered_gateway_tenant_ours_still_comes_from_the_registration(
    world: World,
) -> None:
    open_sub = SUB.model_copy(update={"relay_tenant_id": None, "instance_ids": ()})
    async with make_client(world, sub=open_sub) as client:
        await post(client, world, received(world, tenant_id="anything"))
    (row,) = await stored(world)
    assert row["tenant_id"] == TENANT


async def test_malformed_unknown_or_oversized_requests_are_refused(world: World) -> None:
    async with make_client(world) as client:
        junk = await post(client, world, b"not json at all")
        no_id = await post(client, world, {**received(world), "event_id": ""})
        empty = await post(client, world, received(world, "ev-3", text=None))
        future_schema = await post(client, world, received(world, "ev-4", schema_version=2))
        unknown = await post(client, world, received(world), sub="nope")
        huge = await post(client, world, b"x" * (300 * 1024))
        not_found = await client.get("/webhooks/relayplane/sub-1")
    assert (junk.status_code, no_id.status_code, empty.status_code) == (400, 400, 400)
    assert future_schema.status_code == 400  # a version we do not speak is never half-understood
    assert (unknown.status_code, huge.status_code, not_found.status_code) == (404, 413, 404)
    assert await stored(world) == []


async def test_the_event_id_header_must_agree_with_the_signed_body(world: World) -> None:
    payload = received(world)
    body = json.dumps(payload).encode()
    headers = webhook_headers(SECRET, body, stamp(world), "some-other-event")
    async with make_client(world) as client:
        response = await post(client, world, payload, headers=headers)
    assert response.status_code == 400 and await stored(world) == []


async def test_group_messages_and_unreadable_edits_are_acknowledged_and_ignored(
    world: World,
) -> None:
    async with make_client(world) as client:
        group = await post(client, world, received(world, chat_id="1203@g.us"))
        edit = await post(client, world, received(world, "ev-2", msg_type="secretEncrypted"))
    assert group.json()["reason"] == "group" and edit.json()["reason"] == "unreadable_edit"
    assert await stored(world) == []


async def test_other_event_types_are_acknowledged_and_ignored(world: World) -> None:
    async with make_client(world) as client:
        response = await post(
            client, world, envelope("message.status", {"message_id": "m"}, event_id="ev-1")
        )
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
        down = await post(client, world, received(world))
    async with make_client(world, inbox=Conflict()) as client:
        conflict = await post(client, world, received(world))
    assert down.status_code == 503 and conflict.status_code == 409  # never a 2xx


async def test_an_unconfigured_secret_is_a_server_error_not_a_pass(world: World) -> None:
    async with make_client(world, secrets=InMemorySecretProvider({})) as client:
        response = await post(client, world, received(world))
    assert response.status_code == 503 and await stored(world) == []


async def test_secret_rotation_accepts_either_signature(world: World) -> None:
    payload = received(world)
    body = json.dumps(payload).encode()
    async with make_client(world) as client:
        rotating = webhook_headers("retired-secret", body, stamp(world), "ev-1", SECRET)
        accepted = await post(client, world, payload, headers=rotating)
        strangers = webhook_headers("retired", body, stamp(world), "ev-1", "also-wrong")
        refused = await post(client, world, received(world, "ev-2"), headers=strangers)
    assert accepted.status_code == 200 and refused.status_code == 401


def media(status: str = "READY", **kw: Any) -> dict[str, Any]:
    return {
        "media_id": "med_1",
        "status": status,
        "kind": "audio",
        "mime_type": "audio/ogg",
        "size": 1234,
        "seconds": 8,
        **kw,
    }


async def test_media_arrives_as_a_reference_with_what_the_gateway_made_of_it(
    world: World,
) -> None:
    async with make_client(world) as client:
        ready = await post(client, world, received(world, text=None, media=media()))
        rejected = await post(
            client,
            world,
            received(world, "ev-2", text=None, media=media("REJECTED", reason="TOO_LARGE")),
        )
        failed = await post(
            client, world, received(world, "ev-3", text=None, media=media("FAILED"))
        )
        bogus = await post(client, world, received(world, "ev-4", media=media("TELEPORTED")))
    assert [r.status_code for r in (ready, rejected, failed, bogus)] == [200, 200, 200, 400]
    rows = await stored(world)
    assert [r["media"][0]["status"] for r in rows] == ["ready", "rejected", "failed"]
    assert rows[0]["text"] == "" and rows[0]["media"][0]["media_id"] == "med_1"
    assert rows[1]["media"][0]["reason"] == "TOO_LARGE"
    assert "url" not in rows[0]["media"][0] or rows[0]["media"][0]["url"] is None  # never bytes


async def test_what_became_of_a_send_updates_the_outbox_forward_only(
    world: World, relay: Any
) -> None:
    await world.inbox.insert_if_absent(event("e1", clock=world.clock))
    await world.coordinator("c", FakeLLM([text_response("OlÃ¡!")])).run_once()
    await world.db.pool.execute(
        "UPDATE outbox_messages SET status = 'QUEUED', channel_message_id = 'msg_9'"
    )
    status = {"message_id": "msg_9", "status": "ACCEPTED", "provider_message_id": "3EBOUT"}
    async with make_client(world) as client:
        done = await post(
            client, world, envelope("message.outbound_status", status, event_id="ev-1")
        )
        late = await post(
            client,
            world,
            envelope(
                "message.outbound_status",
                {"message_id": "msg_9", "status": "QUEUED"},
                event_id="ev-2",
            ),
        )
        odd = await post(
            client,
            world,
            envelope(
                "message.outbound_status",
                {"message_id": "msg_9", "status": "TELEPORTED"},
                event_id="ev-3",
            ),
        )
    assert done.json() == {"status": "applied"} and late.json() == {"status": "no_change"}
    assert odd.json()["reason"] == "unknown_status"
    row = await world.db.pool.fetchrow("SELECT * FROM outbox_messages")
    assert row is not None and row["status"] == OutboxStatus.ACCEPTED
    assert row["provider_message_id"] == "3EBOUT"


async def test_a_deleted_message_is_withdrawn_only_while_no_turn_has_taken_it(
    world: World,
) -> None:
    async with make_client(world) as client:
        await post(client, world, received(world, "ev-1"))
        await post(client, world, received(world, "ev-2"))
        gone = await post(
            client,
            world,
            envelope("message.deleted", {"provider_message_id": "3EBev-1"}, event_id="ev-d1"),
        )
        await world.db.pool.execute(
            "UPDATE inbox_events SET status = 'CONSUMED' WHERE event_id = 'ev-2'"
        )
        late = await post(
            client,
            world,
            envelope("message.deleted", {"provider_message_id": "3EBev-2"}, event_id="ev-d2"),
        )
    assert gone.json() == {"status": "applied", "withdrawn": 1}
    assert late.json() == {"status": "applied", "withdrawn": 0}  # already part of a turn
    statuses = {r["event_id"]: r["status"] for r in await stored(world)}
    assert statuses == {"ev-1": "DEAD", "ev-2": "CONSUMED"}


async def test_the_processing_loop_picks_up_what_the_webhook_stored(world: World) -> None:
    async with make_client(world) as client:
        await post(client, world, received(world, text="oi"))
    run = await world.coordinator("w", FakeLLM([text_response("OlÃ¡!")])).run_once()
    assert [r.status for r in run] == ["done"]
    assert await world.count("outbox_messages") == 1


async def test_the_registration_decides_which_runtime_handles_the_conversation(
    world: World,
) -> None:  # INV-057: the scope comes from the operator's subscription, never from the payload
    scoped = SUB.model_copy(update={"scope": "quadras"})
    async with make_client(world, sub=scoped) as client:
        response = await post(client, world, received(world))
    assert response.status_code == 200
    assert await world.db.pool.fetchval("SELECT scope FROM conversation_states") == "quadras"
