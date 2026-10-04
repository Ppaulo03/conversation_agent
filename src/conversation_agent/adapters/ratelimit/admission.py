from __future__ import annotations

from conversation_agent.ports.admission import ADMITTED, AdmissionControl, AdmissionDecision
from conversation_agent.ports.ratelimit import RateLimit, RateLimiter


class ContactRateAdmission:
    """No single contact may flood a tenant: a token bucket per (tenant, contact). Refused
    messages get a 429 and a `Retry-After`; the gateway redelivers, so a human who briefly
    bursts is delayed, not lost. Size the bucket well above what a person types."""

    def __init__(self, limiter: RateLimiter, limit: RateLimit) -> None:
        self._limiter = limiter
        self._limit = limit

    async def admit(self, tenant_id: str, contact_id: str) -> AdmissionDecision:
        decision = await self._limiter.acquire(
            "contact_inbound", f"{tenant_id}/{contact_id}", self._limit
        )
        if decision.allowed:
            return ADMITTED
        wait = decision.retry_after_seconds
        return AdmissionDecision(
            False, 429, int(min(max(wait, 1), 3600)) if wait != float("inf") else 3600,
            "contact_rate_limited",
        )  # fmt: skip


class CompositeAdmission:
    """Several controls, in order: the first refusal wins."""

    def __init__(self, *controls: AdmissionControl) -> None:
        self._controls = controls

    async def admit(self, tenant_id: str, contact_id: str) -> AdmissionDecision:
        for control in self._controls:
            decision = await control.admit(tenant_id, contact_id)
            if not decision.admitted:
                return decision
        return ADMITTED
