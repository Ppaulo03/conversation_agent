"""LLM budgets (Phase 11): pure evaluation, the durable store (audited), where a tenant stands from
the ledger, refusal at the edge only when asked for, and the metric."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.observability.asgi import ops_app
from conversation_agent.adapters.observability.prometheus import PREFIX, available_metrics, render
from conversation_agent.adapters.postgres.audit import PostgresAuditLog
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.health import HealthSnapshot
from conversation_agent.adapters.postgres.llm_budget import (
    BudgetEvaluator,
    LLMBudgetAdmission,
    PostgresBudgetStore,
)
from conversation_agent.adapters.postgres.llm_usage import PostgresLLMUsageStore
from conversation_agent.adapters.relayplane.asgi import webhook_app
from conversation_agent.adapters.relayplane.subscriptions import StaticSubscriptionResolver
from conversation_agent.adapters.relayplane.webhook import RelayPlaneWebhook
from conversation_agent.adapters.secrets.providers import InMemorySecretProvider
from conversation_agent.core.llm_budget import (
    Consumption,
    LLMBudget,
    evaluate,
    period_end,
    period_start,
)
from conversation_agent.core.llm_prices import ModelPrice, PriceTable
from conversation_agent.core.models.llm_usage import LLMCallRecord
from conversation_agent.ports.subscriptions import Subscription
from postgres.world import KEY, World, event
from relayplane_sim.main import message_received, webhook_headers

NOW = datetime(2026, 3, 15, 10, tzinfo=UTC)
PRICES = PriceTable(
    models={
        "m": (ModelPrice(effective_from=date(2026, 1, 1), input_per_mtok=10, output_per_mtok=10),)
    }
)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


# --- the rules (pure) ---


def test_a_budget_needs_a_limit_and_sane_numbers() -> None:
    with pytest.raises(ValidationError, match="at least one limit"):
        LLMBudget()
    for bad in ({"daily_tokens": 0}, {"daily_usd": -1}, {"daily_tokens": 1, "warn_ratio": 1.0}):
        with pytest.raises(ValidationError):
            LLMBudget(**bad)
    with pytest.raises(ValidationError):
        LLMBudget.model_validate({"daily_tokens": 1, "surprise": 1})


def test_periods_are_utc_calendar_days_and_months() -> None:
    assert period_start("day", NOW) == datetime(2026, 3, 15, tzinfo=UTC)
    assert period_end("day", NOW) == datetime(2026, 3, 16, tzinfo=UTC)
    assert period_start("month", NOW) == datetime(2026, 3, 1, tzinfo=UTC)
    assert period_end("month", NOW) == datetime(2026, 4, 1, tzinfo=UTC)
    december = datetime(2026, 12, 31, 23, tzinfo=UTC)
    assert period_end("month", december) == datetime(2027, 1, 1, tzinfo=UTC)


def test_the_state_is_the_worst_line_and_unpriced_usd_is_flagged_as_a_lower_bound() -> None:
    budget = LLMBudget(daily_tokens=1000, monthly_tokens=100_000, daily_usd=10.0)
    ok = evaluate("t", budget, Consumption(tokens=100, usd=1.0), Consumption(tokens=5000), NOW)
    assert ok.state == "ok" and ok.worst_ratio == pytest.approx(0.1)
    warn = evaluate("t", budget, Consumption(tokens=850, usd=1.0), Consumption(), NOW)
    assert warn.state == "warning" and not warn.refuses_new
    over = evaluate(
        "t", budget, Consumption(tokens=100, usd=12.0, unpriced_calls=3), Consumption(), NOW
    )
    assert over.state == "exceeded" and over.resets_at == period_end("day", NOW)
    usd_line = next(line for line in over.lines if line.unit == "usd")
    assert usd_line.lower_bound  # more was spent than the priced part shows
    month_over = evaluate(
        "t", LLMBudget(monthly_tokens=10), Consumption(), Consumption(tokens=10), NOW
    )
    assert month_over.state == "exceeded" and month_over.resets_at == period_end("month", NOW)


def test_only_a_budget_that_asks_for_it_refuses() -> None:
    quiet = evaluate("t", LLMBudget(daily_tokens=1), Consumption(tokens=5), Consumption(), NOW)
    strict = evaluate(
        "t",
        LLMBudget(daily_tokens=1, on_exceed="refuse_new"),
        Consumption(tokens=5),
        Consumption(),
        NOW,
    )
    assert quiet.state == strict.state == "exceeded"
    assert not quiet.refuses_new and strict.refuses_new  # `alert` is the default


def test_defer_llm_only_blocks_model_calls() -> None:
    deferred = evaluate(
        "t",
        LLMBudget(daily_tokens=1, on_exceed="defer_llm"),
        Consumption(tokens=5),
        Consumption(),
        NOW,
    )
    assert deferred.defers_llm and not deferred.refuses_new


# --- the durable store ---


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


async def test_a_budget_is_stored_per_tenant_and_every_change_is_audited(
    db: PostgresDatabase,
) -> None:
    store = PostgresBudgetStore(db)
    assert await store.get("t1") is None
    await store.set(
        "t1", LLMBudget(daily_usd=5.0, on_exceed="refuse_new"), actor="finops@example.com"
    )
    await store.set("t2", LLMBudget(monthly_tokens=1_000_000), actor="finops@example.com")
    assert (await store.get("t1")) == LLMBudget(daily_usd=5.0, on_exceed="refuse_new")
    assert await store.tenants() == ["t1", "t2"]
    await store.set("t1", LLMBudget(daily_usd=7.0), actor="cto@example.com")  # an update
    await store.remove("t2", actor="cto@example.com")
    assert await store.get("t2") is None
    trail = await PostgresAuditLog(db).list("t1", action="llm_budget.set")
    assert [r.actor for r in trail] == ["cto@example.com", "finops@example.com"]
    assert trail[0].details["daily_usd"] == 7.0 and "monthly_tokens" not in trail[0].details
    assert [r.action for r in await PostgresAuditLog(db).list("t2")] == [
        "llm_budget.remove",
        "llm_budget.set",
    ]


# --- where a tenant stands, from the ledger ---


def spend(tokens: int, *, at: datetime = NOW, tenant: str = "t1") -> LLMCallRecord:
    return LLMCallRecord(
        tenant_id=tenant,
        started_at=at,
        provider="p",
        model="m",
        input_tokens=tokens,
        output_tokens=0,
    )


async def standing(
    db: PostgresDatabase, **kw: Any
) -> tuple[BudgetEvaluator, PostgresBudgetStore, PostgresLLMUsageStore, Clock]:
    clock = Clock()
    budgets, usage = PostgresBudgetStore(db), PostgresLLMUsageStore(db)
    evaluator = BudgetEvaluator(
        budgets, usage, PRICES, ttl_seconds=30, monotonic=clock, now=lambda: NOW, **kw
    )
    return evaluator, budgets, usage, clock


async def test_a_tenant_is_measured_against_today_and_this_month_only(db: PostgresDatabase) -> None:
    evaluator, budgets, usage, _ = await standing(db)
    await budgets.set(
        "t1", LLMBudget(daily_tokens=1000, monthly_tokens=10_000, daily_usd=1.0), actor="x"
    )
    for record in (
        spend(300),  # today
        spend(400, at=NOW - timedelta(hours=2)),  # today
        spend(5000, at=NOW - timedelta(days=3)),  # this month, not today
        spend(70_000, at=NOW - timedelta(days=40)),  # last month: outside both
        spend(9_999_999, tenant="other"),  # someone else's
    ):
        await usage.record(record)
    status = await evaluator.status("t1")
    assert status is not None
    by = {(line.period, line.unit): line for line in status.lines}
    assert by[("day", "tokens")].used == 700 and by[("month", "tokens")].used == 5700
    assert by[("day", "usd")].used == pytest.approx(0.007)  # priced at $10 per million
    assert status.state == "ok" and await evaluator.status("nobody") is None


async def test_the_status_is_cached_so_the_edge_does_not_query_every_message(
    db: PostgresDatabase,
) -> None:
    evaluator, budgets, usage, clock = await standing(db)
    await budgets.set("t1", LLMBudget(daily_tokens=1000), actor="x")
    assert (await evaluator.status("t1")).state == "ok"  # type: ignore[union-attr]
    await usage.record(spend(5000))  # the tenant blows through its budget
    assert (await evaluator.status("t1")).state == "ok"  # still the cached answer
    clock.t = 31.0
    assert (await evaluator.status("t1")).state == "exceeded"  # a fresh look


async def test_a_ledger_gap_is_visible_when_a_model_has_no_price(db: PostgresDatabase) -> None:
    evaluator, budgets, usage, _ = await standing(db)
    await budgets.set("t1", LLMBudget(daily_usd=1.0), actor="x")
    await usage.record(
        LLMCallRecord(tenant_id="t1", started_at=NOW, model="mystery", input_tokens=10**9)
    )
    status = await evaluator.status("t1")
    assert status is not None and status.lines[0].used == 0.0 and status.lines[0].lower_bound


# --- refusing at the edge, only when asked ---


async def test_only_a_tenant_over_a_refuse_new_budget_is_refused(db: PostgresDatabase) -> None:
    evaluator, budgets, usage, _ = await standing(db)
    admission = LLMBudgetAdmission(evaluator, now=lambda: NOW)
    await budgets.set("alerting", LLMBudget(daily_tokens=1), actor="x")
    await budgets.set("strict", LLMBudget(daily_tokens=1, on_exceed="refuse_new"), actor="x")
    for tenant in ("alerting", "strict", "unbudgeted"):
        await usage.record(spend(1000, tenant=tenant))
    assert (await admission.admit("alerting", "c")).admitted  # over budget, but it only alerts
    assert (await admission.admit("unbudgeted", "c")).admitted
    refused = await admission.admit("strict", "c")
    assert (refused.admitted, refused.status, refused.reason) == (False, 503, "llm_budget_exceeded")
    assert refused.retry_after_seconds == 3600  # capped; the period ends in 14 h


async def test_the_edge_refuses_a_budget_exceeded_tenant_before_persisting_anything(
    world: World,
) -> None:
    evaluator, budgets, usage, _ = await standing(world.db)
    await budgets.set("tenant-1", LLMBudget(daily_tokens=1, on_exceed="refuse_new"), actor="x")
    await usage.record(spend(1000, tenant="tenant-1"))
    secret = "whsec_budget"
    webhook = RelayPlaneWebhook(
        world.inbox, world.outbox,
        StaticSubscriptionResolver([Subscription(subscription_id="s", tenant_id="tenant-1", secret_ref="wh")]),
        InMemorySecretProvider({("tenant-1", "wh"): secret}), world.clock,
        admission=LLMBudgetAdmission(evaluator, now=lambda: NOW),
    )  # fmt: skip
    payload = message_received("ev-1", timestamp=world.clock.now())
    body = json.dumps(payload).encode()
    headers = webhook_headers(secret, body, int(world.clock.now().timestamp()), "ev-1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=webhook_app(webhook)), base_url="http://hook"
    ) as client:
        response = await client.post("/webhooks/relayplane/s", content=body, headers=headers)
    assert response.status_code == 503 and response.json() == {"error": "llm_budget_exceeded"}
    assert response.headers["retry-after"] and await world.count("inbox_events") == 0


async def test_defer_llm_keeps_the_accepted_turn_until_the_budget_resets(world: World) -> None:
    monotonic = Clock()
    budgets, usage = PostgresBudgetStore(world.db), PostgresLLMUsageStore(world.db)
    now = datetime.now(UTC)
    evaluator = BudgetEvaluator(
        budgets, usage, PRICES, ttl_seconds=30, monotonic=monotonic, now=lambda: now
    )
    await budgets.set("tenant-1", LLMBudget(daily_tokens=1, on_exceed="defer_llm"), actor="x")
    await usage.record(spend(1000, at=now, tenant="tenant-1"))
    await world.inbox.insert_if_absent(event("deferred", "oi", clock=world.clock))
    llm = FakeLLM([text_response("não deve chamar")])

    run = await world.coordinator("w", llm, llm_gate=evaluator).process_conversation(KEY)

    assert run.status == "retry_later" and llm.calls == 0
    assert await world.count("turn_journal", "step_type='LLM_REQUEST'") == 0
    row = await world.db.pool.fetchrow("SELECT deferred_until FROM turns")
    assert row is not None and row["deferred_until"] == period_end("day", now)
    assert await world.inbox.list_ready_conversations() == []  # no hot loop before reset


# --- the metric ---


async def test_the_budget_ratio_is_exposed_per_tenant_period_and_unit(db: PostgresDatabase) -> None:
    evaluator, budgets, usage, _ = await standing(db)
    await budgets.set("t1", LLMBudget(daily_tokens=1000, monthly_usd=100.0), actor="x")
    await usage.record(spend(250))
    statuses = await evaluator.all_statuses()
    text = render(HealthSnapshot(), budgets=statuses)
    assert (
        f'{PREFIX}llm_budget_used_ratio{{tenant_id="t1",period="day",unit="tokens"}} 0.25' in text
    )
    assert (
        f'{PREFIX}llm_budget_used_ratio{{tenant_id="t1",period="month",unit="usd"}} 2.5e-05' in text
    )
    transport = httpx.ASGITransport(app=ops_app(db, budgets=evaluator.all_statuses))
    async with httpx.AsyncClient(transport=transport, base_url="http://ops") as client:
        assert PREFIX + "llm_budget_used_ratio" in (await client.get("/metrics")).text
    assert PREFIX + "llm_budget_used_ratio" in available_metrics()
