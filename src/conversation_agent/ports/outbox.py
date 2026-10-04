from __future__ import annotations

from datetime import timedelta
from typing import Protocol

from conversation_agent.core.models.runtime import OutboundMessage, SendResult


class OutboxStore(Protocol):
    async def claim_ready(
        self, owner: str, limit: int, claim_ttl: timedelta
    ) -> list[OutboundMessage]:
        """PENDING (available) or stale-SENDING rows -> SENDING."""
        ...

    async def record_result(
        self, message: OutboundMessage, owner: str, result: SendResult, retry_after: timedelta
    ) -> None: ...

    async def claim_unknown(
        self, owner: str, limit: int, claim_ttl: timedelta
    ) -> list[OutboundMessage]:
        """UNKNOWN rows due for reconciliation -> RECONCILING (claimed)."""
        ...

    async def record_reconciliation(
        self,
        message: OutboundMessage,
        owner: str,
        *,
        found: SendResult | None,
        resend: bool,
        retry_after: timedelta,
        note: str | None = None,
    ) -> None:
        """`found`: the channel knows the message (record its state). `resend`: the channel
        proved it never received it and the idempotency window is open (-> PENDING, same key).
        Neither: still unproven (-> UNKNOWN, retried later, `reconcile_attempts` + 1)."""
        ...

    async def get(self, tenant_id: str, outbox_id: str) -> OutboundMessage | None: ...
