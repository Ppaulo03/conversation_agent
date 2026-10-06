"""LLM budgets per tenant: how much a tenant may consume per day and per month, and what happens
when it does (DESIGN §37: orçamento de tokens e custo por turno/sessão/tenant).

A budget limits TOKENS (independent of prices) and/or USD (needs the price list; unpriced calls
make the figure a lower bound, which the status says). Evaluation is pure: usage in, status out.

`on_exceed`:
  alert        the default: the status and the metric say so, nothing is refused
  refuse_new   the edge stops taking NEW user messages for this tenant until the period rolls over.
               Be deliberate: a refused message is redelivered by the gateway, but a gateway gives
               up after its own retry budget, so a long refusal can lose messages. It protects the
               bill, not the customer experience.
  defer_llm    accepts inbound messages but pauses only a turn that is about to call an LLM until
                the budget period resets. Deterministic paths may still complete. A confirmation
                that needed LLM interpretation is re-prompted instead of retaining its old answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

State = Literal["ok", "warning", "exceeded"]
Period = Literal["day", "month"]
Unit = Literal["tokens", "usd"]


class LLMBudget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    daily_tokens: int | None = Field(default=None, gt=0)
    monthly_tokens: int | None = Field(default=None, gt=0)
    daily_usd: float | None = Field(default=None, gt=0)
    monthly_usd: float | None = Field(default=None, gt=0)
    warn_ratio: float = Field(default=0.8, gt=0, lt=1)
    on_exceed: Literal["alert", "refuse_new", "defer_llm"] = "alert"

    @model_validator(mode="after")
    def _has_a_limit(self) -> LLMBudget:
        limits = (self.daily_tokens, self.monthly_tokens, self.daily_usd, self.monthly_usd)
        if all(v is None for v in limits):
            raise ValueError("a budget needs at least one limit")
        return self


@dataclass(frozen=True)
class Consumption:
    tokens: int = 0
    usd: float = 0.0
    unpriced_calls: int = 0  # > 0: `usd` is a lower bound


@dataclass(frozen=True)
class BudgetLine:
    period: Period
    unit: Unit
    used: float
    limit: float
    lower_bound: bool = False

    @property
    def ratio(self) -> float:
        return self.used / self.limit


@dataclass(frozen=True)
class BudgetStatus:
    tenant_id: str
    lines: tuple[BudgetLine, ...]
    state: State
    on_exceed: str
    resets_at: datetime  # when the soonest exceeded period starts over (or the day's end)

    @property
    def worst_ratio(self) -> float:
        return max((line.ratio for line in self.lines), default=0.0)

    @property
    def refuses_new(self) -> bool:
        return self.state == "exceeded" and self.on_exceed == "refuse_new"

    @property
    def defers_llm(self) -> bool:
        return self.state == "exceeded" and self.on_exceed == "defer_llm"


def period_start(period: Period, now: datetime) -> datetime:
    now = now.astimezone(UTC)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day if period == "day" else day.replace(day=1)


def period_end(period: Period, now: datetime) -> datetime:
    start = period_start(period, now)
    if period == "day":
        return start + timedelta(days=1)
    return (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )


def evaluate(
    tenant_id: str,
    budget: LLMBudget,
    day: Consumption,
    month: Consumption,
    now: datetime,
) -> BudgetStatus:
    candidates: list[tuple[Period, Unit, float | None, float, bool]] = [
        ("day", "tokens", budget.daily_tokens, day.tokens, False),
        ("month", "tokens", budget.monthly_tokens, month.tokens, False),
        ("day", "usd", budget.daily_usd, day.usd, day.unpriced_calls > 0),
        ("month", "usd", budget.monthly_usd, month.usd, month.unpriced_calls > 0),
    ]
    lines = tuple(
        BudgetLine(period, unit, float(used), float(limit), lower)
        for period, unit, limit, used, lower in candidates
        if limit is not None
    )
    worst = max((line.ratio for line in lines), default=0.0)
    state: State = "exceeded" if worst >= 1.0 else "warning" if worst >= budget.warn_ratio else "ok"
    exceeded = [line.period for line in lines if line.ratio >= 1.0] or ["day"]
    resets = min(period_end(p, now) for p in exceeded)
    return BudgetStatus(tenant_id, lines, state, budget.on_exceed, resets)
