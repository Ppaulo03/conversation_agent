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
from collections.abc import Callable, Hashable
from typing import Any

from conversation_agent.adapters.circuit import CircuitBreaker
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
        self._inner = inner
        self._provider = provider_name
        self._metrics = metrics
        self._breaker = CircuitBreaker(
            failure_threshold=failure_threshold,
            open_seconds=open_seconds,
            half_open_probes=half_open_probes,
            on_state=self._record_state,
            monotonic=monotonic,
        )

    def state(self, tenant_id: str, scope: str) -> str:
        return self._breaker.state((tenant_id, scope))

    def _record_state(self, key: Hashable, state: str) -> None:
        if self._metrics is not None:
            scope = key[1] if isinstance(key, tuple) else str(key)
            self._metrics.record_circuit(provider=self._provider, scope=scope, state=state)

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
        key = (context.tenant_id, binding.tool.connection or binding.tool.name)
        admission = self._breaker.admit(key)
        if admission == "refuse":
            return ToolResult(
                status="technical_error",
                error=ToolError(
                    code="CIRCUIT_OPEN",
                    message_safe="The external system is unavailable for now; nothing was sent.",
                    retryable=True,
                ),
            )
        probing = admission == "probe"
        try:
            result = await self._inner.execute(
                binding, args, context, destination_fingerprint=destination_fingerprint
            )
        except BaseException:
            self._breaker.settle(key, healthy=False, probing=probing)
            raise
        self._breaker.settle(key, healthy=not is_health_failure(result), probing=probing)
        return result


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
