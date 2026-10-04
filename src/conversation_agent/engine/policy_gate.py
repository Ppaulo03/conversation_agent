"""PolicyGate (DESIGN §11): allowlists, effective-risk protection and composable rules.

DENY                  refused before anything runs
REQUIRE_CONFIRMATION  anything that is not a plain read (effective risk, INV-004): it becomes a
                      PendingAction and only runs after an eligible, action-scoped confirmation
ALLOW                 a plain, unconfirmed read
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from conversation_agent.core.definitions.binding import ResolvedToolBinding
from conversation_agent.core.models.tooling import CapabilityRequest, PolicyDecision
from conversation_agent.engine.policy_rules import DEFAULT_RULES, PolicyContext, PolicyRule
from conversation_agent.tools.risk import requires_protection


class PolicyGate:
    def __init__(
        self,
        allowed_capabilities: frozenset[str],
        *,
        tenant_allowlists: Mapping[str, frozenset[str]] | None = None,
        rules: Sequence[PolicyRule] | None = None,
    ) -> None:
        self._allowed = allowed_capabilities
        self._tenant_allowlists = dict(tenant_allowlists or {})
        self._rules = tuple(DEFAULT_RULES if rules is None else rules)

    def allowed_for(self, tenant_id: str) -> frozenset[str]:
        """The agent allowlist, narrowed (never widened) by a tenant-specific allowlist."""
        tenant = self._tenant_allowlists.get(tenant_id)
        return self._allowed if tenant is None else self._allowed & tenant

    def evaluate(
        self,
        capability_name: str,
        resolved: ResolvedToolBinding | None,
        ctx: PolicyContext | None = None,
    ) -> PolicyDecision:
        context = ctx or PolicyContext()
        if capability_name not in self.allowed_for(context.tenant_id) or resolved is None:
            return PolicyDecision(outcome="deny", reason="capability_not_allowed")
        for rule in self._rules:
            denied = rule.check_call(context, capability_name, resolved)
            if denied is not None:
                return denied
        if requires_protection(resolved):
            return PolicyDecision(outcome="require_confirmation", reason="protected_capability")
        return PolicyDecision(outcome="allow", reason="read_capability")

    def refine(
        self,
        decision: PolicyDecision,
        request: CapabilityRequest,
        resolved: ResolvedToolBinding,
        ctx: PolicyContext | None = None,
    ) -> PolicyDecision:
        """Rules that need the validated, canonical request. A DENY here overrides everything
        (including a pending confirmation: a forbidden action is not even proposed)."""
        context = ctx or PolicyContext()
        for rule in self._rules:
            denied = rule.check_request(context, request, resolved)
            if denied is not None:
                return denied
        return decision
