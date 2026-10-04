from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from conversation_agent.adapters.postgres.audit import insert_audit
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.llm_budget import (
    BudgetStatus,
    Consumption,
    LLMBudget,
    evaluate,
    period_start,
)
from conversation_agent.core.llm_prices import PriceTable
from conversation_agent.core.models.audit import AuditEntry
from conversation_agent.core.models.llm_usage import UsageQuery, UsageRow
from conversation_agent.ports.admission import ADMITTED, AdmissionDecision
from conversation_agent.ports.llm_usage import LLMUsageStore


class PostgresBudgetStore:
    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db

    async def get(self, tenant_id: str) -> LLMBudget | None:
        raw = await self._db.pool.fetchval(
            "SELECT budget FROM llm_budgets WHERE tenant_id = $1", tenant_id
        )
        return LLMBudget.model_validate(raw) if raw else None

    async def tenants(self) -> list[str]:
        rows = await self._db.pool.fetch("SELECT tenant_id FROM llm_budgets ORDER BY tenant_id")
        return [r["tenant_id"] for r in rows]

    async def set(self, tenant_id: str, budget: LLMBudget, *, actor: str) -> None:
        async with self._db.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO llm_budgets (tenant_id, budget, updated_by) VALUES ($1,$2,$3) "
                "ON CONFLICT (tenant_id) DO UPDATE SET budget = $2, updated_by = $3, "
                "updated_at = clock_timestamp()",
                tenant_id,
                budget.model_dump(mode="json"),
                actor,
            )
            await insert_audit(
                conn,
                AuditEntry(
                    tenant_id=tenant_id,
                    actor=actor,
                    action="llm_budget.set",
                    subject_type="tenant",
                    subject_id=tenant_id,
                    details={
                        k: v for k, v in budget.model_dump(mode="json").items() if v is not None
                    },
                ),
            )

    async def remove(self, tenant_id: str, *, actor: str) -> None:
        async with self._db.pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM llm_budgets WHERE tenant_id = $1", tenant_id)
            await insert_audit(
                conn,
                AuditEntry(
                    tenant_id=tenant_id,
                    actor=actor,
                    action="llm_budget.remove",
                    subject_type="tenant",
                    subject_id=tenant_id,
                ),
            )


def _consumption(rows: list[UsageRow]) -> Consumption:
    return Consumption(
        tokens=sum(
            r.input_tokens + r.output_tokens + r.cache_read_tokens + r.cache_write_tokens
            for r in rows
        ),
        usd=sum(r.cost_usd for r in rows),
        unpriced_calls=sum(r.unpriced_calls for r in rows),
    )


class BudgetEvaluator:
    """Where a tenant stands against its budget, from the usage ledger.

    Evaluated from the ledger (indexed by tenant and time) and cached per tenant for `ttl` seconds,
    so checking on every inbound message does not become load of its own; the price of that is that
    a tenant can overshoot by what it spends within one cache window.
    """

    def __init__(
        self,
        budgets: PostgresBudgetStore,
        usage: LLMUsageStore,
        prices: PriceTable,
        *,
        ttl_seconds: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._budgets = budgets
        self._usage = usage
        self._prices = prices
        self._ttl = ttl_seconds
        self._monotonic = monotonic
        self._now = now
        self._cache: dict[str, tuple[float, BudgetStatus | None]] = {}

    async def status(self, tenant_id: str) -> BudgetStatus | None:
        """None when the tenant has no budget."""
        cached = self._cache.get(tenant_id)
        if cached is not None and self._monotonic() - cached[0] < self._ttl:
            return cached[1]
        budget = await self._budgets.get(tenant_id)
        status: BudgetStatus | None = None
        if budget is not None:
            now = self._now()
            day = await self._used(tenant_id, period_start("day", now), now)
            month = await self._used(tenant_id, period_start("month", now), now)
            status = evaluate(tenant_id, budget, day, month, now)
        self._cache[tenant_id] = (self._monotonic(), status)
        return status

    async def all_statuses(self) -> list[BudgetStatus]:
        out: list[BudgetStatus] = []
        for tenant in await self._budgets.tenants():
            status = await self.status(tenant)
            if status is not None:
                out.append(status)
        return out

    async def _used(self, tenant_id: str, since: datetime, until: datetime) -> Consumption:
        rows = await self._usage.report(
            UsageQuery(
                tenant_id=tenant_id, since=since, until=until.replace(microsecond=0) + _SECOND
            ),
            self._prices,
        )
        return _consumption(rows)


_SECOND = timedelta(seconds=1)


class LLMBudgetAdmission:
    """Refuses NEW user messages for a tenant whose budget is exceeded AND whose policy says
    `refuse_new`. Everything else (no budget, `alert`, under the limit) is admitted."""

    def __init__(
        self,
        evaluator: BudgetEvaluator,
        *,
        max_retry_after_seconds: int = 3600,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._evaluator = evaluator
        self._max_retry = max_retry_after_seconds
        self._now = now

    async def admit(self, tenant_id: str, contact_id: str) -> AdmissionDecision:
        status = await self._evaluator.status(tenant_id)
        if status is None or not status.refuses_new:
            return ADMITTED
        wait = int((status.resets_at - self._now()).total_seconds())
        return AdmissionDecision(
            False, 503, max(1, min(wait, self._max_retry)), "llm_budget_exceeded"
        )
