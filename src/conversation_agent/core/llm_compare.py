"""Comparing what two versions of an agent cost per unit of work (the question a canary asks).

Total spend says nothing when the versions served different amounts of traffic: what matters is cost
and model calls PER TURN. A release that answers the same questions with twice the calls is a
regression the eval suite (scripted) cannot see and the ledger (real) can.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class VersionCost(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_version: str
    turns: int = 0
    conversations: int = 0
    calls: int = 0
    errors: int = 0
    tokens: int = 0
    cost_usd: float = 0.0  # the priced part; a lower bound when `unpriced_calls` > 0
    unpriced_calls: int = 0
    latency_p95_ms: float = 0.0

    @property
    def cost_per_turn(self) -> float:
        return self.cost_usd / self.turns if self.turns else 0.0

    @property
    def calls_per_turn(self) -> float:
        return self.calls / self.turns if self.turns else 0.0

    @property
    def error_rate(self) -> float:
        return self.errors / self.calls if self.calls else 0.0


def cost_regressions(
    stable: VersionCost,
    candidate: VersionCost,
    *,
    max_ratio: float = 1.25,
    min_turns: int = 30,
) -> list[str]:
    """Why `candidate` looks worse than `stable` (empty: nothing to object to). Not enough turns on
    either side is itself reported: a verdict from a handful of turns is noise."""
    found: list[str] = []
    for side in (stable, candidate):
        if side.turns < min_turns:
            found.append(f"{side.agent_version}: only {side.turns} turns (need {min_turns})")
    if found:
        return found
    if stable.unpriced_calls or candidate.unpriced_calls:
        found.append("some calls have no price: the cost comparison is a lower bound")
    if stable.cost_per_turn > 0 and candidate.cost_per_turn > stable.cost_per_turn * max_ratio:
        found.append(
            f"cost per turn {candidate.cost_per_turn:.5f} vs {stable.cost_per_turn:.5f} "
            f"(more than x{max_ratio})"
        )
    if stable.calls_per_turn > 0 and candidate.calls_per_turn > stable.calls_per_turn * max_ratio:
        found.append(
            f"{candidate.calls_per_turn:.2f} model calls per turn vs {stable.calls_per_turn:.2f} "
            f"(more than x{max_ratio})"
        )
    if candidate.error_rate > max(stable.error_rate * 2, 0.02):
        found.append(f"error rate {candidate.error_rate:.1%} vs {stable.error_rate:.1%} on stable")
    return found
