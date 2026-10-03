"""Effective risk/confirmation helpers (DESIGN §5.2). The binding can never lower protection."""

from __future__ import annotations

from conversation_agent.core.definitions.binding import ResolvedToolBinding


def requires_protection(resolved: ResolvedToolBinding) -> bool:
    return resolved.effective_risk != "read" or resolved.effective_confirmation_required
