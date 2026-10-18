"""Composition-root helper: wires logging, tracing and the metrics in one call.

    obs = setup_observability()                       # JSON logs, span lines, price list
    llm = obs.wrap_llm(real_llm, usage_store)         # every call on the books, with cost
    http = obs.wrap_tools(HTTPToolProvider(...), "http")  # counted, circuit-breaker friendly
    app = obs.ops_app(db, budgets=evaluator.all_statuses)  # /healthz /readyz /metrics

Environment (all optional): LOG_LEVEL (info), LOG_STACK (0/1: include scrubbed stack traces),
TRACE (log | otlp | off), OTEL_SERVICE_NAME, standard OTEL_EXPORTER_OTLP_* settings,
LLM_PRICES_FILE (ops/llm_prices.yaml).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from conversation_agent.adapters.llm.metered import MeteredLLMProvider
from conversation_agent.adapters.llm.metrics import InMemoryLLMMetrics
from conversation_agent.adapters.observability.asgi import ops_app
from conversation_agent.adapters.observability.logs import configure_logging
from conversation_agent.adapters.observability.prices import load_prices
from conversation_agent.adapters.observability.runtime_metrics import InMemoryRuntimeMetrics
from conversation_agent.adapters.observability.tracing import LogTracer
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.tools.metrics import InMemoryToolMetrics
from conversation_agent.adapters.tools.resilience import MeteredToolProvider
from conversation_agent.core.llm_budget import BudgetStatus
from conversation_agent.core.llm_prices import PriceTable
from conversation_agent.core.tracing import configure_tracer
from conversation_agent.ports.llm import LLMProvider
from conversation_agent.ports.llm_usage import LLMUsageStore
from conversation_agent.ports.tool_provider import ToolProvider


@dataclass
class Observability:
    prices: PriceTable
    tool_metrics: InMemoryToolMetrics = field(default_factory=InMemoryToolMetrics)
    llm_metrics: InMemoryLLMMetrics = field(default_factory=InMemoryLLMMetrics)
    runtime_metrics: InMemoryRuntimeMetrics = field(default_factory=InMemoryRuntimeMetrics)
    _tracing_shutdown: Callable[[], None] | None = field(default=None, repr=False)

    def wrap_llm(
        self,
        inner: LLMProvider,
        usage: LLMUsageStore | None = None,
        *,
        provider_name: str = "unknown",
        model_name: str = "unknown",
    ) -> MeteredLLMProvider:
        """The provider every turn should use: usage on the books, counters, estimated cost."""
        return MeteredLLMProvider(
            inner,
            usage,
            metrics=self.llm_metrics,
            prices=self.prices,
            provider_name=provider_name,
            model_name=model_name,
        )

    def wrap_tools(self, inner: ToolProvider, provider_name: str) -> MeteredToolProvider:
        return MeteredToolProvider(inner, self.tool_metrics, provider_name=provider_name)

    def ops_app(
        self,
        db: PostgresDatabase,
        *,
        budgets: Callable[[], Awaitable[list[BudgetStatus]]] | None = None,
    ) -> Callable[[Any, Any, Any], Awaitable[None]]:
        return ops_app(db, self.tool_metrics, self.llm_metrics, self.runtime_metrics, budgets)

    def shutdown(self) -> None:
        """Flush optional telemetry. Call during deployment shutdown."""
        configure_tracer(None)
        if self._tracing_shutdown is not None:
            self._tracing_shutdown()


def setup_observability(
    *,
    environ: Mapping[str, str] | None = None,
    stream: Any = None,
) -> Observability:
    env = os.environ if environ is None else environ
    level = getattr(logging, env.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    configure_logging(level, include_stack=env.get("LOG_STACK", "0") == "1", stream=stream)
    trace_mode = env.get("TRACE", "log").lower()
    tracing_shutdown: Callable[[], None] | None = None
    if trace_mode == "log":
        configure_tracer(LogTracer())
    elif trace_mode == "off":
        configure_tracer(None)
    elif trace_mode == "otlp":
        from conversation_agent.adapters.observability.opentelemetry import build_otlp_tracing

        tracing = build_otlp_tracing(
            service_name=env.get("OTEL_SERVICE_NAME", "conversation-agent")
        )
        configure_tracer(tracing.tracer)
        tracing_shutdown = tracing.shutdown
    else:
        raise ValueError("TRACE must be log, otlp or off")
    prices_path = Path(env.get("LLM_PRICES_FILE", "ops/llm_prices.yaml"))
    prices = load_prices(prices_path) if prices_path.exists() else PriceTable()
    return Observability(prices=prices, _tracing_shutdown=tracing_shutdown)
