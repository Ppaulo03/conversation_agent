"""Which capabilities a contact cannot use right now (ADR-033).

The agent's allowlist says what the AGENT may do; this port narrows it for ONE contact: a finance
tool for a contact whose account is not linked, a feature the contact has not paid for. A capability
named here is not shown to the model, its prompt sections are left out, and the PolicyGate refuses
it if it is called anyway (the prompt is never the only defence, INV-004). The port can only
REMOVE: it can never add or authorize a capability.
"""

from __future__ import annotations

from typing import Protocol


class CapabilityAvailability(Protocol):
    async def unavailable(self, tenant_id: str, channel_id: str, contact_id: str) -> frozenset[str]:
        """Names of the capabilities this contact cannot use. Called once per turn, its answer is
        journaled (a replay reuses it). Names the agent does not have are ignored. If it raises,
        the framework behaves as if nothing were restricted: the tools themselves still decide."""
        ...
