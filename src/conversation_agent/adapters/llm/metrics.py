from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

# Upper bounds (seconds) of the latency histogram: an LLM call lives between ~0.3 s and a minute.
LATENCY_BUCKETS: tuple[float, ...] = (0.25, 0.5, 1, 2, 4, 8, 16, 32, 64)


@dataclass
class Histogram:
    buckets: tuple[float, ...] = LATENCY_BUCKETS
    counts: list[int] = field(default_factory=list)  # cumulative per bucket, then +Inf
    total: float = 0.0
    count: int = 0

    def __post_init__(self) -> None:
        self.counts = [0] * (len(self.buckets) + 1)

    def observe(self, value: float) -> None:
        self.total += value
        self.count += 1
        for i, bound in enumerate(self.buckets):
            if value <= bound:
                self.counts[i] += 1
        self.counts[-1] += 1  # +Inf


@dataclass
class InMemoryLLMMetrics:
    """Counters in process memory (scraped per instance; Prometheus aggregates)."""

    calls: Counter[tuple[str, str, str, str, str]] = field(default_factory=Counter)
    tokens: Counter[tuple[str, str, str, str]] = field(default_factory=Counter)
    cost_usd: dict[tuple[str, str, str], float] = field(default_factory=dict)
    unpriced: Counter[tuple[str, str]] = field(default_factory=Counter)
    latency: dict[tuple[str, str, str], Histogram] = field(default_factory=dict)
    usage_write_failures: int = 0

    def record_llm_call(
        self,
        *,
        provider: str,
        model: str,
        purpose: str,
        outcome: str,
        error_code: str | None,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        duration_seconds: float,
        cost_usd: float | None,
    ) -> None:
        self.calls[(provider, model, purpose, outcome, error_code or "")] += 1
        for kind, amount in (
            ("input", input_tokens),
            ("output", output_tokens),
            ("cache_read", cache_read_tokens),
            ("cache_write", cache_write_tokens),
        ):
            if amount:
                self.tokens[(provider, model, purpose, kind)] += amount
        if cost_usd is None:
            self.unpriced[(provider, model)] += 1
        else:
            key = (provider, model, purpose)
            self.cost_usd[key] = self.cost_usd.get(key, 0.0) + cost_usd
        self.latency.setdefault((provider, model, purpose), Histogram()).observe(duration_seconds)

    def record_usage_write_failure(self) -> None:
        self.usage_write_failures += 1
