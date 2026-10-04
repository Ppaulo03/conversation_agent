"""Per-provider metrics and circuit breaking, as decorators over any ToolProvider (DESIGN §37).

Both are TRANSPARENT to the runtime's guarantees:

  - a breaker fails fast only BEFORE calling the inner provider, so nothing was sent and the
    answer (`technical_error CIRCUIT_OPEN`, retryable) is a known non-execution, safe for a read
    and for a write alike (INV-038). Whatever the inner provider answers is passed through
    UNCHANGED: a breaker never reclassifies an `unknown` write as anything else;
  - only provider HEALTH counts against a circuit (unreachable, timeouts, 5xx, unusable
    responses, `unknown`). A business or validation answer proves the system is alive; a
    configuration problem (bad URL, missing secret) is not the destination's fault;
  - the circuit is per (tenant, connection): one tenant's broken ERP does not stop another's.
    State is in memory, per process.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from conversation_agent.core.definitions.binding import ResolvedToolBinding
from conversation_agent.core.models.tooling import ToolContext, ToolError, ToolResult
from conversation_agent.ports.metrics import ToolMetrics
from conversation_agent.ports.ratelimit import RateLimit, RateLimiter
from conversation_agent.ports.tool_provider import ToolProvider

# Canonical codes that say "the destination is unwell" (see tools.error_mapping and the providers).
HEALTH_FAILURE_CODES = frozenset(
    {
        "EXTERNAL_UNREACHABLE",
        "EXTERNAL_TIMEOUT",
        "EXTERNAL_5XX",
        "EXTERNAL_RATE_LIMITED",
        "EXTERNAL_INVALID_RESPONSE",
        "MCP_SERVER_ERROR",
        "MCP_SESSION_UNAVAILABLE",
        "MCP_HANDSHAKE_FAILED",
        "MCP_DISCOVERY_FAILED",
    }
)


def is_health_failure(result: ToolResult) -> bool:
    if result.status in ("timeout", "unknown"):
        return True
    return (
        result.status == "technical_error"
        and result.error is not None
        and result.error.code in HEALTH_FAILURE_CODES
    )


@dataclass
class _Circuit:
    state: str = "closed"
    failures: int = 0
    opened_at: float = 0.0
    probes: int = 0


class CircuitBreakerToolProvider:
    def __init__(
        self,
        inner: ToolProvider,
        *,
        provider_name: str,
        failure_threshold: int = 5,
        open_seconds: float = 30.0,
        half_open_probes: int = 1,
        metrics: ToolMetrics | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1 or half_open_probes < 1 or open_seconds <= 0:
            raise ValueError("the circuit needs a positive threshold, window and probe count")
        self._inner = inner
        self._provider = provider_name
        self._threshold = failure_threshold
        self._open_seconds = open_seconds
        self._probes = half_open_probes
        self._metrics = metrics
        self._now = monotonic
        self._circuits: dict[tuple[str, str], _Circuit] = {}

    def state(self, tenant_id: str, scope: str) -> str:
        return self._circuits.get((tenant_id, scope), _Circuit()).state

    async def destination_fingerprint(
        self, binding: ResolvedToolBinding, context: ToolContext
    ) -> str | None:
        return await self._inner.destination_fingerprint(binding, context)

    async def execute(
        self,
        binding: ResolvedToolBinding,
        args: dict[str, Any],
        context: ToolContext,
        *,
        destination_fingerprint: str | None = None,
    ) -> ToolResult:
        scope = binding.tool.connection or binding.tool.name
        circuit = self._circuits.setdefault((context.tenant_id, scope), _Circuit())
        if not self._admit(circuit, scope):
            return ToolResult(
                status="technical_error",
                error=ToolError(
                    code="CIRCUIT_OPEN",
                    message_safe="The external system is unavailable for now; nothing was sent.",
                    retryable=True,
                ),
            )
        probing = circuit.state == "half_open"
        try:
            result = await self._inner.execute(
                binding, args, context, destination_fingerprint=destination_fingerprint
            )
        except BaseException:
            self._settle(circuit, scope, healthy=False, probing=probing)
            raise
        self._settle(circuit, scope, healthy=not is_health_failure(result), probing=probing)
        return result

    # ------------------------------------------------------------------ state machine

    def _admit(self, circuit: _Circuit, scope: str) -> bool:
        if circuit.state == "open":
            if self._now() - circuit.opened_at < self._open_seconds:
                return False
            self._move(circuit, scope, "half_open")
            circuit.probes = 0
        if circuit.state == "half_open":
            if circuit.probes >= self._probes:
                return False
            circuit.probes += 1
        return True

    def _settle(self, circuit: _Circuit, scope: str, *, healthy: bool, probing: bool) -> None:
        if healthy:
            circuit.failures = 0
            if probing and circuit.state == "half_open":
                self._move(circuit, scope, "closed")
            return
        circuit.failures += 1
        if probing or circuit.failures >= self._threshold:
            circuit.opened_at = self._now()
            if circuit.state != "open":
                self._move(circuit, scope, "open")

    def _move(self, circuit: _Circuit, scope: str, state: str) -> None:
        circuit.state = state
        if state == "closed":
            circuit.failures = 0
        if self._metrics is not None:
            try:
                self._metrics.record_circuit(provider=self._provider, scope=scope, state=state)
            except Exception:  # metrics must never change what a tool call does
                return


class MeteredToolProvider:
    """Records one metric per call: provider, tool, canonical status/code and duration. Never the
    arguments or the data."""

    def __init__(
        self,
        inner: ToolProvider,
        metrics: ToolMetrics,
        *,
        provider_name: str,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._metrics = metrics
        self._provider = provider_name
        self._now = monotonic

    async def destination_fingerprint(
        self, binding: ResolvedToolBinding, context: ToolContext
    ) -> str | None:
        return await self._inner.destination_fingerprint(binding, context)

    async def execute(
        self,
        binding: ResolvedToolBinding,
        args: dict[str, Any],
        context: ToolContext,
        *,
        destination_fingerprint: str | None = None,
    ) -> ToolResult:
        started = self._now()
        try:
            result = await self._inner.execute(
                binding, args, context, destination_fingerprint=destination_fingerprint
            )
        except BaseException:
            self._record(binding, "exception", "PROVIDER_RAISED", started)
            raise
        self._record(binding, result.status, result.error.code if result.error else None, started)
        return result

    def _record(
        self, binding: ResolvedToolBinding, status: str, code: str | None, started: float
    ) -> None:
        try:
            self._metrics.record_call(
                provider=self._provider,
                tool=binding.tool.name,
                status=status,
                error_code=code,
                duration_ms=(self._now() - started) * 1000,
            )
        except Exception:  # metrics must never change what a tool call does
            return


class RateLimitedToolProvider:
    """A cap on calls per (tenant, connection): protects the external system AND the other
    tenants sharing this runtime from one tenant's burst. Like the circuit breaker it acts only
    BEFORE calling the inner provider, so a denial is a known non-execution (`technical_error
    RATE_LIMITED`, retryable) that is safe for a write too, and everything the provider answers
    passes through unchanged."""

    def __init__(
        self,
        inner: ToolProvider,
        limiter: RateLimiter,
        limit: RateLimit,
        *,
        per_connection: dict[str, RateLimit] | None = None,
    ) -> None:
        self._inner = inner
        self._limiter = limiter
        self._limit = limit
        self._per_connection = per_connection or {}

    async def destination_fingerprint(
        self, binding: ResolvedToolBinding, context: ToolContext
    ) -> str | None:
        return await self._inner.destination_fingerprint(binding, context)

    async def execute(
        self,
        binding: ResolvedToolBinding,
        args: dict[str, Any],
        context: ToolContext,
        *,
        destination_fingerprint: str | None = None,
    ) -> ToolResult:
        scope = binding.tool.connection or binding.tool.name
        limit = self._per_connection.get(scope, self._limit)
        decision = await self._limiter.acquire(
            "tool_connection", f"{context.tenant_id}/{scope}", limit
        )
        if not decision.allowed:
            return ToolResult(
                status="technical_error",
                error=ToolError(
                    code="RATE_LIMITED",
                    message_safe="Too many requests to the external system; nothing was sent.",
                    retryable=True,
                ),
                provider_metadata={"retry_after_seconds": round(decision.retry_after_seconds, 1)},
            )
        return await self._inner.execute(
            binding, args, context, destination_fingerprint=destination_fingerprint
        )
