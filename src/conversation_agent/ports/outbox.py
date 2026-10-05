from __future__ import annotations

from datetime import timedelta
from typing import Protocol

from conversation_agent.core.models.runtime import OutboundMessage, SendResult


class OutboxStore(Protocol):
    async def claim_ready(
        self, owner: str, limit: int, claim_ttl: timedelta, scope: str | None = None
    ) -> list[OutboundMessage]:
        """PENDING (available) or stale-SENDING rows -> SENDING."""
        ...

    async def record_result(
        self, message: OutboundMessage, owner: str, result: SendResult, retry_after: timedelta
    ) -> None: ...

    async def claim_unsettled(
        self,
        owner: str,
        limit: int,
        claim_ttl: timedelta,
        poll_after: timedelta,
        scope: str | None = None,
    ) -> list[OutboundMessage]:
        """Rows whose outcome the channel has not told us yet -> RECONCILING (claimed): UNKNOWN
        ones that are due, and QUEUED ones that nobody has heard about for `poll_after` (the
        status event may have been lost)."""
        ...

    async def apply_channel_status(
        self, tenant_id: str, channel_message_id: str, result: SendResult
    ) -> bool:
        """The channel told us (event or poll) what became of a send. Only moves a row FORWARD
        (a late QUEUED never undoes an ACCEPTED, a FAILED never undoes a final ACCEPTED).
        Returns whether a row was updated."""
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
