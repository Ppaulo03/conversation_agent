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

    async def get(self, tenant_id: str, outbox_id: str) -> OutboundMessage | None: ...
