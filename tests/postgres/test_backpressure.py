"""Capacity and backpressure (DESIGN §37): rate limits that hold across workers, shedding at the
edge without losing anything, and per-tenant caps on outbound tool calls that never make a write
ambiguous."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import httpx
import pytest

from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.postgres.admission import InboxBacklogAdmission
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.ratelimit import PostgresRateLimiter
from conversation_agent.adapters.ratelimit.admission import CompositeAdmission, ContactRateAdmission
from conversation_agent.adapters.ratelimit.memory import InMemoryRateLimiter
from conversation_agent.adapters.relayplane.asgi import webhook_app
from conversation_agent.adapters.relayplane.subscriptions import StaticSubscriptionResolver
from conversation_agent.adapters.relayplane.webhook import RelayPlaneWebhook
from conversation_agent.adapters.secrets.providers import InMemorySecretProvider
from conversation_agent.adapters.tools.fake import FakeToolProvider
from conversation_agent.adapters.tools.resilience import RateLimitedToolProvider
from conversation_agent.core.models.tooling import ToolContext, ToolError, ToolResult
from conversation_agent.ports.admission import ADMITTED, AdmissionDecision
from conversation_agent.ports.ratelimit import RateLimit
from conversation_agent.ports.subscriptions import Subscription
from postgres.world import World, event
from relayplane_sim.main import envelope, message_received, webhook_headers

# --- the limiter, in memory and shared ---


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


BURST = RateLimit(capacity=3, refill_per_second=1)


async def test_the_bucket_allows_a_burst_then_says_how_long_to_wait_and_refills() -> None:
    clock = Clock()
    limiter = InMemoryRateLimiter(clock)
    assert all([(await limiter.acquire("s", "k", BURST)).allowed for _ in range(3)])
    denied = await limiter.acquire("s", "k", BURST)
    assert not denied.allowed and denied.retry_after_seconds == pytest.approx(1.0)
    clock.t = 1.0
    assert (await limiter.acquire("s", "k", BURST)).allowed  # one token came back
    assert not (await limiter.acquire("s", "k", BURST)).allowed
    clock.t = 100.0
    assert (await limiter.acquire("s", "k", BURST, cost=3)).allowed  # never above the capacity
    assert not (await limiter.acquire("s", "k", BURST, cost=1)).allowed


async def test_scopes_and_keys_are_independent_and_an_impossible_cost_never_fits() -> None:
    limiter = InMemoryRateLimiter(Clock())
    for _ in range(3):
        await limiter.acquire("contact", "ana", BURST)
    assert not (await limiter.acquire("contact", "ana", BURST)).allowed
    assert (await limiter.acquire("contact", "bia", BURST)).allowed  # another key
    assert (await limiter.acquire("tool", "ana", BURST)).allowed  # another scope
    impossible = await limiter.acquire("x", "y", BURST, cost=4)
    assert not impossible.allowed and impossible.retry_after_seconds == float("inf")


def test_a_limit_needs_positive_numbers() -> None:
    for bad in ((0, 1), (1, 0), (-1, 1)):
        with pytest.raises(ValueError):
            RateLimit(*bad)


async def test_the_shared_limiter_holds_across_concurrent_workers(db: PostgresDatabase) -> None:
    limits = RateLimit(capacity=5, refill_per_second=0.001)
    workers = [PostgresRateLimiter(db) for _ in range(4)]  # four "processes", one database
    results = await asyncio.gather(
        *(workers[i % 4].acquire("contact", "ana", limits) for i in range(40))
    )
    assert sum(r.allowed for r in results) == 5  # exactly the bucket, not 5 per worker
    assert all(r.retry_after_seconds > 0 for r in results if not r.allowed)


async def test_the_shared_limiter_refills_by_the_database_clock(db: PostgresDatabase) -> None:
    limiter = PostgresRateLimiter(db)
    fast = RateLimit(capacity=1, refill_per_second=50)
    assert (await limiter.acquire("s", "k", fast)).allowed
    assert not (await limiter.acquire("s", "k", fast)).allowed
    await asyncio.sleep(0.1)
    assert (await limiter.acquire("s", "k", fast)).allowed
    assert not (await limiter.acquire("s", "k", fast, cost=2)).allowed
    assert (await limiter.acquire("s", "k", fast, cost=2)).retry_after_seconds == float("inf")
    assert await limiter.purge(timedelta(0)) == 1  # untouched buckets can be forgotten
    assert await limiter.purge(timedelta(0)) == 0


# --- admission ---


async def test_a_contact_that_floods_gets_a_429_with_a_retry_hint() -> None:
    clock = Clock()
    admission = ContactRateAdmission(InMemoryRateLimiter(clock), RateLimit(2, 0.5))
    assert (await admission.admit("t", "ana")).admitted and (
        await admission.admit("t", "ana")
    ).admitted
    refused = await admission.admit("t", "ana")
    assert (refused.admitted, refused.status, refused.reason) == (
        False,
        429,
        "contact_rate_limited",
    )
    assert refused.retry_after_seconds == 2
    assert (await admission.admit("t", "bia")).admitted  # one contact does not slow another


async def test_the_edge_sheds_while_the_tenant_is_behind_and_recovers(
    db: PostgresDatabase, clock: FixedClock
) -> None:
    world = World(db, clock)
    now = Clock()
    gate = InboxBacklogAdmission(db, max_ready=3, retry_after_seconds=7, monotonic=now)
    assert (await gate.admit("tenant-1", "ana")).admitted
    for i in range(3):
        await world.inbox.insert_if_absent(event(f"b{i}", "oi", clock=clock))
    now.t = 5.0  # past the cache
    refused = await gate.admit("tenant-1", "ana")
    assert (refused.admitted, refused.status, refused.retry_after_seconds) == (False, 503, 7)
    assert (await gate.admit("another-tenant", "ana")).admitted  # per tenant
    await db.pool.execute("UPDATE inbox_events SET status = 'CONSUMED'")
    assert not (await gate.admit("tenant-1", "ana")).admitted  # cached: no query per request
    now.t = 10.0
    assert (await gate.admit("tenant-1", "ana")).admitted  # the workers caught up


async def test_a_message_waiting_too_long_also_counts_as_behind(
    db: PostgresDatabase, clock: FixedClock
) -> None:
    world = World(db, clock)
    await world.inbox.insert_if_absent(event("old", "oi", clock=clock))
    await db.pool.execute(
        "UPDATE inbox_events SET received_at = clock_timestamp() - interval '1 hour'"
    )
    gate = InboxBacklogAdmission(db, max_ready=1000, max_oldest_age=timedelta(minutes=10))
    assert (await gate.admit("tenant-1", "x")).reason == "inbox_backlog"


async def test_the_first_refusal_wins_in_a_composite() -> None:
    class Always:
        def __init__(self, decision: AdmissionDecision) -> None:
            self.decision, self.asked = decision, 0

        async def admit(self, tenant_id: str, contact_id: str) -> AdmissionDecision:
            self.asked += 1
            return self.decision

    no = Always(AdmissionDecision(False, 503, 9, "behind"))
    never_asked = Always(ADMITTED)
    composite = CompositeAdmission(Always(ADMITTED), no, never_asked)
    assert (await composite.admit("t", "c")).reason == "behind" and never_asked.asked == 0
    assert (await CompositeAdmission().admit("t", "c")).admitted


# --- the webhook sheds without losing ---

SECRET = "whsec_test"
SUB = Subscription(subscription_id="sub-1", tenant_id="tenant-1", secret_ref="wh")


class Door:
    """An admission control we can open and close."""

    def __init__(self) -> None:
        self.open = False

    async def admit(self, tenant_id: str, contact_id: str) -> AdmissionDecision:
        return ADMITTED if self.open else AdmissionDecision(False, 503, 30, "inbox_backlog")


def client_for(world: World, door: Door) -> httpx.AsyncClient:
    webhook = RelayPlaneWebhook(
        world.inbox,
        world.outbox,
        StaticSubscriptionResolver([SUB]),
        InMemorySecretProvider({("tenant-1", "wh"): SECRET}),
        world.clock,
        admission=door,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=webhook_app(webhook)), base_url="http://hook"
    )


async def deliver(
    client: httpx.AsyncClient, world: World, payload: dict[str, Any]
) -> httpx.Response:
    body = json.dumps(payload).encode()
    headers = webhook_headers(SECRET, body, int(world.clock.now().timestamp()), payload["event_id"])
    return await client.post("/webhooks/relayplane/sub-1", content=body, headers=headers)


async def test_an_overloaded_edge_refuses_without_persisting_and_the_redelivery_is_accepted(
    db: PostgresDatabase, clock: FixedClock
) -> None:
    world, door = World(db, clock), Door()
    payload = message_received("ev-1", timestamp=clock.now())
    async with client_for(world, door) as client:
        refused = await deliver(client, world, payload)
        assert refused.status_code == 503 and refused.headers["retry-after"] == "30"
        assert refused.json() == {"error": "inbox_backlog"}
        assert await world.count("inbox_events") == 0  # nothing acknowledged, nothing stored

        door.open = True  # the gateway redelivers later: same event, same id
        accepted = await deliver(client, world, payload)
    assert accepted.status_code == 200 and accepted.json() == {"status": "accepted"}
    assert await world.count("inbox_events") == 1  # delayed, never lost, never duplicated


async def test_only_new_user_messages_are_shed_state_updates_always_get_through(
    db: PostgresDatabase, clock: FixedClock
) -> None:
    world, door = World(db, clock), Door()  # the door stays shut
    await world.db.pool.execute("SELECT 1")
    async with client_for(world, door) as client:
        status = {"message_id": "msg_9", "status": "ACCEPTED", "provider_message_id": "3EB1"}
        applied = await deliver(
            client, world, envelope("message.outbound_status", status, event_id="ev-s")
        )
        deleted = await deliver(
            client,
            world,
            envelope("message.deleted", {"provider_message_id": "3EBx"}, event_id="ev-d"),
        )
    assert applied.status_code == 200 and deleted.status_code == 200  # never blocked by the shed


async def test_a_flooding_contact_is_limited_at_the_edge_end_to_end(
    db: PostgresDatabase, clock: FixedClock
) -> None:
    world = World(db, clock)
    admission = ContactRateAdmission(PostgresRateLimiter(db), RateLimit(2, 0.001))
    webhook = RelayPlaneWebhook(
        world.inbox, world.outbox, StaticSubscriptionResolver([SUB]),
        InMemorySecretProvider({("tenant-1", "wh"): SECRET}), world.clock, admission=admission,
    )  # fmt: skip
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=webhook_app(webhook)), base_url="http://hook"
    ) as client:
        codes = [
            (
                await deliver(client, world, message_received(f"ev-{i}", timestamp=clock.now()))
            ).status_code
            for i in range(5)
        ]
    assert codes == [200, 200, 429, 429, 429] and await world.count("inbox_events") == 2


# --- outbound tool calls ---

CONTEXT = ToolContext(
    tenant_id="t1", agent_id="a", agent_version="1", channel_id="c", conversation_id="c1",
    session_id="s", contact_id="p", turn_id="turn", invocation_id="inv", trace_id="tr",
)  # fmt: skip


async def test_a_tenant_burst_is_capped_before_anything_is_sent_even_for_a_write() -> None:
    inner = FakeToolProvider({"mcp_create_ticket": ToolResult(status="success", data={"ok": 1})})
    limited = RateLimitedToolProvider(inner, InMemoryRateLimiter(Clock()), RateLimit(2, 0.1))
    binding = binding_for_tool("write")
    results = [await limited.execute(binding, {}, CONTEXT) for _ in range(4)]
    assert [r.status for r in results] == [
        "success",
        "success",
        "technical_error",
        "technical_error",
    ]
    assert len(inner.calls) == 2  # the denied ones never reached the provider
    denied = results[2]
    assert (
        denied.error is not None and denied.error.code == "RATE_LIMITED" and denied.error.retryable
    )
    assert denied.provider_metadata["retry_after_seconds"] > 0  # known non-execution, not `unknown`


async def test_the_cap_is_per_tenant_and_per_connection_and_answers_pass_through() -> None:
    boom = ToolResult(status="unknown", error=ToolError(code="EXTERNAL_TIMEOUT", message_safe="x"))
    inner = FakeToolProvider({"mcp_create_ticket": boom})
    limited = RateLimitedToolProvider(
        inner, InMemoryRateLimiter(Clock()), RateLimit(1, 0.1),
        per_connection={"vip": RateLimit(5, 1)},
    )  # fmt: skip
    first = await limited.execute(binding_for_tool("write"), {}, CONTEXT)
    assert first is boom  # whatever the provider answers is untouched
    assert (
        await limited.execute(binding_for_tool("write"), {}, CONTEXT)
    ).status == "technical_error"
    other_tenant = CONTEXT.model_copy(update={"tenant_id": "t2"})
    assert (await limited.execute(binding_for_tool("write"), {}, other_tenant)) is boom
    vip = binding_for_tool("write", connection="vip")
    assert all([(await limited.execute(vip, {}, CONTEXT)) is boom for _ in range(5)])


def binding_for_tool(risk: str, connection: str = "support_mcp") -> Any:
    from pydantic import BaseModel

    from conversation_agent.core.definitions.binding import CapabilityBinding, ResolvedToolBinding
    from conversation_agent.core.definitions.capability import CapabilityDefinition
    from conversation_agent.core.definitions.tool import ToolDefinition

    class Args(BaseModel):
        pass

    capability = CapabilityDefinition(
        name="a.b",
        description="x",
        input_model=Args,
        output_model=Args,
        risk=risk,  # type: ignore[arg-type]
    )
    tool = ToolDefinition(
        name="mcp_create_ticket", description="x", input_model=Args, risk=risk,  # type: ignore[arg-type]
        provider="fake", connection=connection,
    )  # fmt: skip
    return ResolvedToolBinding(
        capability=capability,
        tool=tool,
        binding=CapabilityBinding(capability="a.b", tool=tool.name, input_map={}, output_map={}),
    )
