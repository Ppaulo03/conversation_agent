from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass
class LatencySummary:
    count: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0

    @property
    def mean_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0


@dataclass
class InMemoryToolMetrics:
    """Counters in process memory: for tests, the CLI and as the reference for a real backend."""

    calls: Counter[tuple[str, str, str, str | None]] = field(default_factory=Counter)
    latency: dict[tuple[str, str], LatencySummary] = field(default_factory=dict)
    circuits: list[tuple[str, str, str]] = field(default_factory=list)

    def record_call(
        self,
        *,
        provider: str,
        tool: str,
        status: str,
        error_code: str | None,
        duration_ms: float,
    ) -> None:
        self.calls[(provider, tool, status, error_code)] += 1
        summary = self.latency.setdefault((provider, tool), LatencySummary())
        summary.count += 1
        summary.total_ms += duration_ms
        summary.max_ms = max(summary.max_ms, duration_ms)

    def record_circuit(self, *, provider: str, scope: str, state: str) -> None:
        self.circuits.append((provider, scope, state))
