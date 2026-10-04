from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AdmissionDecision:
    """Whether the edge should take one more inbound message right now."""

    admitted: bool
    status: int = 200  # when refused: 429 (this contact/tenant is too fast) or 503 (we are behind)
    retry_after_seconds: int = 0
    reason: str = ""


ADMITTED = AdmissionDecision(True)


class AdmissionControl(Protocol):
    """Backpressure at the edge. Refusing is SAFE for an at-least-once channel: nothing is
    persisted, nothing is acknowledged, the gateway redelivers later. Never used to drop work
    that was already accepted."""

    async def admit(self, tenant_id: str, contact_id: str) -> AdmissionDecision: ...
