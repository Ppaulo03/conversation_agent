"""The integration example (`examples/relayplane-app`) as a consumer would run it: one ASGI app,
started by its lifespan, against PostgreSQL and the gateway simulator over real HTTP."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, MutableMapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from conftest import ApiHandle, RelayHandle
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.postgres.db import PostgresDatabase
from relayplane_app.app import ASGIApp, Settings, create_app
from relayplane_sim.main import envelope, message_received, webhook_headers

SECRET = "whsec_app"


def settings(dsn: str, relay: RelayHandle, api: ApiHandle, **overrides: Any) -> Settings:
    base = Settings(
        database_url=dsn,
        relayplane_url=relay.base_url,
        relayplane_api_key="rp_test",
        webhook_secret=SECRET,
        subscription_id="sub-1",
        tenant_id="tenant-1",
        scheduling_api_url=api.base_url,
        insecure_local=True,
    )
    return Settings(**{**base.__dict__, **overrides})


@asynccontextmanager
async def lifespan(app: ASGIApp) -> AsyncIterator[str]:
    """Drive the ASGI lifespan the way a server does; yields 'complete' or the failure message."""
    inbox: asyncio.Queue[MutableMapping[str, Any]] = asyncio.Queue()
    outbox: asyncio.Queue[MutableMapping[str, Any]] = asyncio.Queue()

    async def receive() -> MutableMapping[str, Any]:
        return await inbox.get()

    async def send(message: MutableMapping[str, Any]) -> None:
        await outbox.put(message)

    task = asyncio.create_task(app({"type": "lifespan"}, receive, send))
    await inbox.put({"type": "lifespan.startup"})
    started = await asyncio.wait_for(outbox.get(), 10)
    try:
        yield (
            started.get("message", "complete") if started["type"].endswith("failed") else "complete"
        )
    finally:
        if not task.done():
            await inbox.put({"type": "lifespan.shutdown"})
            assert (await asyncio.wait_for(outbox.get(), 10))[
                "type"
            ] == "lifespan.shutdown.complete"
        await task


async def post_event(
    client: httpx.AsyncClient, payload: dict[str, Any], secret: str = SECRET
) -> httpx.Response:
    body = json.dumps(payload).encode()
    now = int(datetime.now(UTC).timestamp())
    headers = webhook_headers(secret, body, now, payload["event_id"])
    return await client.post("/webhooks/relayplane/sub-1", content=body, headers=headers)


async def wait_until(condition: Any, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await condition():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.05)


async def test_a_message_goes_in_through_the_webhook_and_the_reply_reaches_the_gateway(
    db: PostgresDatabase, pg_dsn: str, relay: RelayHandle, api: ApiHandle
) -> None:
    llm = FakeLLM([text_response("Abrimos de segunda a sábado, das 8h às 18h.")])
    app = create_app(settings(pg_dsn, relay, api), llm=llm, poll_interval=0.05)
    async with (
        lifespan(app) as started,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://agent"
        ) as client,
    ):
        assert started == "complete"
        assert (await client.get("/readyz")).json() == {"status": "ready"}

        event = message_received("ev-1", text="vocês abrem sábado?", timestamp=datetime.now(UTC))
        assert (await post_event(client, event)).json() == {"status": "accepted"}
        assert (await post_event(client, event)).json() == {"status": "duplicate"}

        async def delivered() -> bool:
            return len(relay.sent) == 1

        await wait_until(delivered)
        (sent,) = relay.sent
        assert sent["payload"] == {"text": "Abrimos de segunda a sábado, das 8h às 18h."}
        assert sent["to"] == "5511999990000" and sent["instance_id"] == "inst_1"

        async def queued() -> bool:
            return await db.pool.fetchval("SELECT status FROM outbox_messages") == "QUEUED"

        await wait_until(queued)
        relay.settle("msg_1", "ACCEPTED", provider_message_id="3EBOUT1")
        status = envelope(
            "message.outbound_status",
            {"message_id": "msg_1", "status": "ACCEPTED", "provider_message_id": "3EBOUT1"},
            event_id="ev-status",
        )
        assert (await post_event(client, status)).json() == {"status": "applied"}
        assert await db.pool.fetchval("SELECT status FROM outbox_messages") == "ACCEPTED"

        metrics = await client.get("/metrics")
        assert metrics.status_code == 200 and "conversation_agent_outbox_unknown 0" in metrics.text
        assert (await client.get("/anything-else")).status_code == 404


async def test_a_webhook_signed_with_another_secret_is_refused_and_nothing_is_stored(
    db: PostgresDatabase, pg_dsn: str, relay: RelayHandle, api: ApiHandle
) -> None:
    app = create_app(settings(pg_dsn, relay, api), llm=FakeLLM([]), poll_interval=0.05)
    async with (
        lifespan(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://agent"
        ) as client,
    ):
        event = message_received("ev-1", timestamp=datetime.now(UTC))
        assert (await post_event(client, event, secret="whsec_other")).status_code == 401
    assert await db.pool.fetchval("SELECT count(*) FROM inbox_events") == 0


async def test_it_refuses_to_start_when_the_resend_window_outlives_the_gateway_key_retention(
    db: PostgresDatabase, pg_dsn: str, relay: RelayHandle, api: ApiHandle
) -> None:
    # the simulator remembers keys for 24 h; resending for 48 h could duplicate a message
    app = create_app(
        settings(pg_dsn, relay, api, retry_horizon=timedelta(hours=48)), llm=FakeLLM([])
    )
    async with lifespan(app) as started:
        assert started != "complete" and "retry_horizon" in started
