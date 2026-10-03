"""PolicyGate (DESIGN §11). Phase 1 scope: allowlist + protection gate.

Anything that is not a plain, unconfirmed read is never ALLOWed here: it requires
confirmation, a machinery that arrives in Phase 3. Until then it is only recorded as a
draft proposal (see TurnEngine) and never executed.
"""

from __future__ import annotations

from conversation_agent.core.definitions.binding import ResolvedToolBinding
from conversation_agent.core.models.tooling import PolicyDecision
from conversation_agent.tools.risk import requires_protection


class PolicyGate:
    def __init__(self, allowed_capabilities: frozenset[str]) -> None:
        self._allowed = allowed_capabilities

    def evaluate(
        self, capability_name: str, resolved: ResolvedToolBinding | None
    ) -> PolicyDecision:
        if capability_name not in self._allowed or resolved is None:
            return PolicyDecision(outcome="deny", reason="capability_not_allowed")
        if requires_protection(resolved):
            return PolicyDecision(outcome="require_confirmation", reason="protected_capability")
        return PolicyDecision(outcome="allow", reason="read_capability")
