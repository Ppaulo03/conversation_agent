from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from conversation_agent.adapters.llm.metrics import Histogram

# A turn takes from a fraction of a second (a Flow) to many seconds (an agent loop with tools).
TURN_BUCKETS: tuple[float, ...] = (0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64, 128)


def _histogram() -> Histogram:
    return Histogram(buckets=TURN_BUCKETS)


@dataclass
class InMemoryRuntimeMetrics:
    """Hot-path counters in process memory (scraped per instance; Prometheus aggregates)."""

    turns: Counter[tuple[str, str, str]] = field(default_factory=Counter)  # agent, version, outcome
    processing: dict[tuple[str, str], Histogram] = field(default_factory=dict)
    queue_wait: dict[tuple[str, str], Histogram] = field(default_factory=dict)
    llm_calls: Counter[tuple[str, str]] = field(default_factory=Counter)
    proposals: Counter[tuple[str, str]] = field(default_factory=Counter)
    handoffs: Counter[tuple[str, str]] = field(default_factory=Counter)

    def record_turn(
        self,
        *,
        agent_id: str,
        agent_version: str,
        outcome: str,
        processing_seconds: float,
        queue_seconds: float | None,
        llm_calls: int,
        proposed: int,
    ) -> None:
        who = (agent_id, agent_version)
        self.turns[(*who, outcome)] += 1
        self.processing.setdefault(who, _histogram()).observe(processing_seconds)
        if queue_seconds is not None:
            self.queue_wait.setdefault(who, _histogram()).observe(max(queue_seconds, 0.0))
        self.llm_calls[who] += llm_calls
        self.proposals[who] += proposed

    def record_handoff(self, *, agent_id: str, agent_version: str) -> None:
        self.handoffs[(agent_id, agent_version)] += 1
