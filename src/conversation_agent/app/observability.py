"""Composition-root helper: wires logging, tracing and the metrics in one call.

    obs = setup_observability()                       # JSON logs, span lines, price list
    llm = obs.wrap_llm(real_llm, usage_store)         # every call on the books, with cost
    http = obs.wrap_tools(HTTPToolProvider(...), "http")  # counted, circuit-breaker friendly
    app = obs.ops_app(db, budgets=evaluator.all_statuses)  # /healthz /readyz /metrics

Environment (all optional): LOG_LEVEL (info), LOG_STACK (0/1: include scrubbed stack traces),
TRACE (log | off), LLM_PRICES_FILE (ops/llm_prices.yaml).
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


def setup_observability(
    *,
    environ: Mapping[str, str] | None = None,
    stream: Any = None,
) -> Observability:
    env = os.environ if environ is None else environ
    level = getattr(logging, env.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    configure_logging(level, include_stack=env.get("LOG_STACK", "0") == "1", stream=stream)
    configure_tracer(LogTracer() if env.get("TRACE", "log") == "log" else None)
    prices_path = Path(env.get("LLM_PRICES_FILE", "ops/llm_prices.yaml"))
    prices = load_prices(prices_path) if prices_path.exists() else PriceTable()
    return Observability(prices=prices)
