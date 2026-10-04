"""MeteredLLMProvider: every model call is measured and written to the books.

A decorator over any `LLMProvider`. It reads WHO and WHY from the observability context the runtime
binds (tenant, agent version, turn, purpose), times the call, and records one `LLMCallRecord` with
the tokens the provider reported, success or failure. It also feeds the Prometheus counters and the
estimated cost.

It never changes what a call does: whatever the provider returns (or raises) passes through, and a
failure to WRITE the record is logged and counted, not propagated: losing a usage row must not turn
a customer's answer into an error. (A call made outside any bound context is recorded under
`_unattributed`, so spend is visible even when attribution is not.)

Because it sits where the provider is called, a journal REPLAY (which reuses the stored response
and never reaches the provider) is not counted again, and a call repeated after a crash IS counted
twice, because the provider really billed twice (DESIGN: no exactly-once billing).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from conversation_agent.core.errors import LLMProviderError
from conversation_agent.core.llm_prices import PriceTable, cost_of
from conversation_agent.core.models.llm import LLMRequest, LLMResponse
from conversation_agent.core.models.llm_usage import UNATTRIBUTED_TENANT, LLMCallRecord
from conversation_agent.core.observability import current
from conversation_agent.ports.llm import LLMProvider
from conversation_agent.ports.llm_usage import LLMUsageStore
from conversation_agent.ports.metrics import LLMMetrics

log = logging.getLogger(__name__)


class MeteredLLMProvider:
    def __init__(
        self,
        inner: LLMProvider,
        store: LLMUsageStore | None = None,
        *,
        metrics: LLMMetrics | None = None,
        prices: PriceTable | None = None,
        provider_name: str = "unknown",
        model_name: str = "unknown",
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._inner = inner
        self._store = store
        self._metrics = metrics
        self._prices = prices
        # Used only when the provider does not say who answered (fakes, replays).
        self._provider_name = provider_name
        self._model_name = model_name
        self._monotonic = monotonic
        self._now = now

    async def complete(self, request: LLMRequest) -> LLMResponse:
        started_at, started = self._now(), self._monotonic()
        try:
            response = await self._inner.complete(request)
        except LLMProviderError as exc:
            await self._record(request, started_at, started, None, "error", type(exc).__name__)
            raise
        except Exception as exc:  # a provider bug: still a call that happened
            await self._record(request, started_at, started, None, "error", type(exc).__name__)
            raise
        await self._record(request, started_at, started, response, "ok", None)
        return response

    async def _record(
        self,
        request: LLMRequest,
        started_at: datetime,
        started: float,
        response: LLMResponse | None,
        outcome: str,
        error_code: str | None,
    ) -> None:
        context = current()
        usage = response.usage if response is not None else None
        provider = (response.provider if response else None) or self._provider_name
        model = (response.model if response else None) or self._model_name
        record = LLMCallRecord(
            tenant_id=context.get("tenant_id", UNATTRIBUTED_TENANT),
            started_at=started_at,
            purpose=context.get("purpose", "agent"),
            agent_id=context.get("agent_id"),
            agent_version=context.get("agent_version"),
            conversation_ref=context.get("conversation_ref"),
            turn_id=context.get("turn_id"),
            trace_id=context.get("trace_id"),
            request_id=request.request_id,
            provider=provider,
            model=model,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            cache_read_tokens=usage.cache_read_tokens if usage else 0,
            cache_write_tokens=usage.cache_write_tokens if usage else 0,
            reasoning_tokens=usage.reasoning_tokens if usage else 0,
            latency_ms=(self._monotonic() - started) * 1000,
            outcome="ok" if outcome == "ok" else "error",
            error_code=error_code,
            stop_reason=response.stop_reason.value if response else None,
        )
        if self._metrics is not None:
            self._observe(record)
        if self._store is not None:
            try:
                await self._store.record(record)
            except Exception as exc:  # never let the books break the answer
                log.error(
                    "llm_usage.record_failed",
                    extra={"fields": {"alert": True, "error": type(exc).__name__}},
                )
                if self._metrics is not None:
                    self._metrics.record_usage_write_failure()

    def _observe(self, record: LLMCallRecord) -> None:
        assert self._metrics is not None
        cost = (
            cost_of(
                self._prices,
                provider=record.provider,
                model=record.model,
                at=record.started_at,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                cache_read_tokens=record.cache_read_tokens,
                cache_write_tokens=record.cache_write_tokens,
            )
            if self._prices is not None
            else None
        )
        try:
            self._metrics.record_llm_call(
                provider=record.provider or "unknown",
                model=record.model or "unknown",
                purpose=record.purpose,
                outcome=record.outcome,
                error_code=record.error_code,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                cache_read_tokens=record.cache_read_tokens,
                cache_write_tokens=record.cache_write_tokens,
                duration_seconds=record.latency_ms / 1000,
                cost_usd=cost,
            )
        except Exception:  # metrics never change what a call does
            return
