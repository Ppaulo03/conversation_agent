"""Composable policy rules (DESIGN §11). The prompt is never the only defence of an invariant.

Two phases per call:
  check_call     before argument validation: who/what is asking (allowlist, ownership, budgets)
  check_request  after validation, on the canonical request (loops, argument limits, schedule)
A rule returns a DENY decision, or None to abstain. The first DENY wins.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from conversation_agent.core.definitions.binding import ResolvedToolBinding
from conversation_agent.core.models.media import MediaArg
from conversation_agent.core.models.runtime import Ownership
from conversation_agent.core.models.tooling import CapabilityRequest, PolicyDecision


@dataclass(frozen=True)
class PolicyContext:
    """Runtime-known facts about the call. Never supplied by the LLM."""

    tenant_id: str = ""
    ownership: Ownership = Ownership.BOT
    tool_calls_this_turn: int = 0
    recent_args_hashes: tuple[str, ...] = field(default_factory=tuple)  # earlier calls this turn
    now: datetime | None = None  # the turn's reference time
    media: Mapping[str, MediaArg] = field(default_factory=dict)  # handle -> the conversation's file


def _deny(reason: str) -> PolicyDecision:
    return PolicyDecision(outcome="deny", reason=reason)


class PolicyRule(Protocol):
    def check_call(
        self, ctx: PolicyContext, capability: str, resolved: ResolvedToolBinding
    ) -> PolicyDecision | None: ...

    def check_request(
        self, ctx: PolicyContext, request: CapabilityRequest, resolved: ResolvedToolBinding
    ) -> PolicyDecision | None: ...


class _Base:
    def check_call(
        self, ctx: PolicyContext, capability: str, resolved: ResolvedToolBinding
    ) -> PolicyDecision | None:
        return None

    def check_request(
        self, ctx: PolicyContext, request: CapabilityRequest, resolved: ResolvedToolBinding
    ) -> PolicyDecision | None:
        return None


class OwnershipRule(_Base):
    """HUMAN / HANDOFF_PENDING never trigger automation (INV-019, defence in depth)."""

    def check_call(self, ctx, capability, resolved):  # type: ignore[no-untyped-def]
        if ctx.ownership is not Ownership.BOT:
            return _deny("conversation_not_bot_owned")
        return None


class ToolCallBudgetRule(_Base):
    def __init__(self, max_calls: int = 12) -> None:
        self._max = max_calls

    def check_call(self, ctx, capability, resolved):  # type: ignore[no-untyped-def]
        if ctx.tool_calls_this_turn >= self._max:
            return _deny("tool_call_budget_exceeded")
        return None


class LoopRule(_Base):
    """The same capability with the same arguments again and again is a loop, not progress."""

    def __init__(self, max_repeats: int = 3) -> None:
        self._max = max_repeats

    def check_request(self, ctx, request, resolved):  # type: ignore[no-untyped-def]
        if ctx.recent_args_hashes.count(request.args_hash) >= self._max:
            return _deny("loop_detected")
        return None


class NumericLimitRule(_Base):
    """E.g. a monetary ceiling: refuse `capability` when `field` exceeds `maximum`."""

    def __init__(self, capability: str, field_name: str, maximum: float) -> None:
        self._capability = capability
        self._field = field_name
        self._maximum = maximum

    def check_request(self, ctx, request, resolved):  # type: ignore[no-untyped-def]
        if request.capability != self._capability:
            return None
        value = request.args.get(self._field)
        if isinstance(value, int | float) and value > self._maximum:
            return _deny(f"limit_exceeded:{self._field}")
        return None


class AllowedHoursRule(_Base):
    """Only allow `capability` between start_hour (incl) and end_hour (excl) local time."""

    def __init__(self, capability: str, start_hour: int, end_hour: int, timezone: str) -> None:
        self._capability = capability
        self._start, self._end = start_hour, end_hour
        self._tz = ZoneInfo(timezone)

    def check_request(self, ctx, request, resolved):  # type: ignore[no-untyped-def]
        if request.capability != self._capability or ctx.now is None:
            return None
        hour = ctx.now.astimezone(self._tz).hour
        if not (self._start <= hour < self._end):
            return _deny("outside_allowed_hours")
        return None


DEFAULT_RULES: tuple[PolicyRule, ...] = (OwnershipRule(), ToolCallBudgetRule(12), LoopRule(3))
