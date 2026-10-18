"""An application that puts a conversation_agent agent on WhatsApp through RelayPlane.

The framework ships no entrypoint (ADR-022): the application composes it. This is that composition,
kept small enough to copy. One ASGI app does three things:

  POST /webhooks/relayplane/{subscription_id}   signed gateway events -> durable inbox
  GET  /healthz, /readyz, /metrics              operational endpoints (keep them off the internet)
  background                                    `Runtime.run`: turns, outbox -> gateway, timers,
                                                reconciliation

It runs the reference scheduling agent (`examples/vertical-slice`); swap `load_compiled_agent` and
the tool connections for your own. Run it with any ASGI server, for example

    uv run --env-file .env uvicorn relayplane_app.app:app --port 8002

after applying the migrations once (`python -m conversation_agent.app.migrate apply`): the app
checks the schema at startup and refuses to start on a mismatch, it never migrates by itself.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from conversation_agent.adapters.clock import SystemClock
from conversation_agent.adapters.connections.static import StaticConnectionResolver
from conversation_agent.adapters.observability.asgi import ops_app
from conversation_agent.adapters.postgres.coordination import CoordinationTime
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.outbox import PostgresOutboxStore
from conversation_agent.adapters.relayplane.asgi import webhook_app
from conversation_agent.adapters.relayplane.subscriptions import StaticSubscriptionResolver
from conversation_agent.adapters.relayplane.webhook import RelayPlaneWebhook
from conversation_agent.adapters.secrets.providers import InMemorySecretProvider
from conversation_agent.adapters.senders.relayplane import RelayPlaneSender
from conversation_agent.adapters.tools.http import HTTPToolProvider
from conversation_agent.app.llm_factory import LLMConfig, build_llm, close_llm
from conversation_agent.app.runtime import Runtime
from conversation_agent.core.compiler import CompiledAgent
from conversation_agent.core.models.connections import AuthSpec, ResolvedConnection
from conversation_agent.ports.llm import LLMProvider
from conversation_agent.ports.subscriptions import Subscription
from vertical_slice.definitions import CONNECTION
from vertical_slice.wiring import load_compiled_agent

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

WEBHOOK_PREFIX = "/webhooks/relayplane/"
OPS_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})
API_KEY_REF = "relayplane-api-key"
WEBHOOK_SECRET_REF = "relayplane-webhook-secret"


@dataclass(frozen=True)
class Settings:
    """Everything the operator decides, read from the environment by `from_env`."""

    database_url: str
    relayplane_url: str
    relayplane_api_key: str
    webhook_secret: str  # the subscription's `whsec_...`
    subscription_id: str
    tenant_id: str  # OUR tenant; the payload never chooses it
    scheduling_api_url: str
    relay_tenant_id: str | None = None  # when set, events from another gateway tenant are refused
    retry_horizon: timedelta = timedelta(hours=1)  # must fit inside the gateway's key retention
    timezone: str = "America/Sao_Paulo"
    # Plain HTTP and private addresses for the gateway and the tool API: a local sandbox ONLY.
    insecure_local: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        relay_tenant = env.get("RELAYPLANE_TENANT_ID") or None
        return cls(
            database_url=env["DATABASE_URL"],
            relayplane_url=env["RELAYPLANE_BASE_URL"],
            relayplane_api_key=env["RELAYPLANE_API_KEY"],
            webhook_secret=env["RELAYPLANE_WEBHOOK_SECRET"],
            subscription_id=env["RELAYPLANE_SUBSCRIPTION_ID"],
            tenant_id=env["TENANT_ID"],
            scheduling_api_url=env["SCHEDULING_API_URL"],
            relay_tenant_id=relay_tenant,
            retry_horizon=timedelta(seconds=int(env.get("RETRY_HORIZON_SECONDS", "3600"))),
            timezone=env.get("TIMEZONE", "America/Sao_Paulo"),
            insecure_local=env.get("INSECURE_LOCAL") == "1",
        )


def _connection(
    settings: Settings, connection_id: str, url: str, **extra: Any
) -> ResolvedConnection:
    local = settings.insecure_local
    return ResolvedConnection(
        connection_id=connection_id,
        base_url=url,
        allow_private_networks=local,
        tls_required=not local,
        **extra,
    )


def create_app(
    settings: Settings,
    *,
    llm: LLMProvider | None = None,
    compiled: CompiledAgent | None = None,
    poll_interval: float = 0.3,
) -> ASGIApp:
    """The ASGI app. `llm` defaults to the provider configured by `LLM_*` (`app.llm_factory`);
    `compiled` to the reference scheduling agent. Nothing connects until the server's lifespan
    starts, so building the app is free."""
    secrets = InMemorySecretProvider(  # swap for your secret manager
        {
            (settings.tenant_id, API_KEY_REF): settings.relayplane_api_key,
            (settings.tenant_id, WEBHOOK_SECRET_REF): settings.webhook_secret,
        }
    )
    relay = _connection(
        settings, "relayplane", settings.relayplane_url, auth=AuthSpec(secret_ref=API_KEY_REF)
    )
    sender = RelayPlaneSender(StaticConnectionResolver({"relayplane": relay}), secrets)
    agent = compiled or load_compiled_agent()
    subscription = Subscription(
        subscription_id=settings.subscription_id,
        tenant_id=settings.tenant_id,
        secret_ref=WEBHOOK_SECRET_REF,
        relay_tenant_id=settings.relay_tenant_id,
        scope=agent.agent_id,  # the runtime below handles the conversations of this agent
    )
    live: dict[str, Any] = {}

    async def startup() -> None:
        db = await PostgresDatabase.connect(settings.database_url)
        live["db"] = db
        await db.ensure_schema_current()  # read-only: a stale schema stops the deploy here
        # The resend window comes from what the DEPLOYED gateway reports (`GET /limits`), so a
        # gateway with a shorter idempotency retention refuses to start instead of duplicating.
        policy = await sender.delivery_policy(settings.tenant_id, settings.retry_horizon)
        model = llm or build_llm(LLMConfig.from_env(os.environ))
        live["owned_llm"] = None if llm else model
        http = HTTPToolProvider.static(
            {CONNECTION: _connection(settings, CONNECTION, settings.scheduling_api_url)}
        )
        live["http"] = http
        runtime = Runtime.build(
            db=db,
            compiled=agent,
            llm=model,
            providers={"http": http},
            sender=sender,
            delivery_policy=policy,
        )
        clock = SystemClock(settings.timezone)
        webhook = RelayPlaneWebhook(
            runtime.inbox,
            PostgresOutboxStore(db, clock, coordination=CoordinationTime(db)),
            StaticSubscriptionResolver([subscription]),
            secrets,
            clock,
        )
        live["webhook"] = webhook_app(webhook)
        live["ops"] = ops_app(db)
        live["stop"] = stop = asyncio.Event()
        live["task"] = asyncio.create_task(runtime.run(stop, poll_interval=poll_interval))

    async def shutdown() -> None:
        if "stop" in live:
            live["stop"].set()
            await live["task"]  # the current turn reaches a safe point; nothing is cut midway
        if "http" in live:
            await live["http"].aclose()
        if live.get("owned_llm") is not None:
            await close_llm(live["owned_llm"])
        await sender.aclose()
        if "db" in live:
            await live["db"].close()

    async def not_found(send: Send) -> None:
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    try:
                        await startup()
                    except Exception as exc:
                        await shutdown()
                        await send({"type": "lifespan.startup.failed", "message": repr(exc)})
                        return
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await shutdown()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http" or "webhook" not in live:
            return await not_found(send)
        path = scope["path"]
        if path.startswith(WEBHOOK_PREFIX):
            await live["webhook"](scope, receive, send)
        elif path in OPS_PATHS:
            await live["ops"](scope, receive, send)
        else:
            await not_found(send)

    return app


def _app_from_env() -> ASGIApp | None:
    try:
        return create_app(Settings.from_env(os.environ))
    except KeyError:
        return None  # imported without configuration (tests, tooling): build it with create_app


app = _app_from_env()
