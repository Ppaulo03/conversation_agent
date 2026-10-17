from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from conversation_agent.core.models.runtime import OutboundMessage, SendResult


class OutboxStore(Protocol):
    async def claim_ready(
        self,
        owner: str,
        limit: int,
        claim_ttl: timedelta,
        scope: str | None = None,
        *,
        unknown_blocks_for: timedelta,
    ) -> list[OutboundMessage]:
        """PENDING (available) or stale-SENDING rows -> SENDING, never ahead of an earlier message
        of the same conversation that is still waiting to go out (see the adapter). An unresolved
        UNKNOWN holds later messages back for `unknown_blocks_for` (the delivery policy's safe
        resend window: the caller passes it, the store has no default). The returned rows say
        what they were (`claimed_from`)."""
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
        *,
        with_handle: bool = True,
    ) -> list[OutboundMessage]:
        """Rows whose outcome the channel has not told us yet -> RECONCILING (claimed): UNKNOWN
        ones that are due, and QUEUED ones that nobody has heard about for `poll_after` (the
        status event may have been lost). QUEUED rows always have a handle. `with_handle=False`
        (no lookup is configured) claims only UNKNOWN rows WITHOUT a handle: the ones the
        reconciler can still act on; those with a handle wait for the channel to push."""
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
        resend_until: datetime | None = None,
    ) -> None:
        """`found`: the channel knows the message (record its state). `resend`: the channel
        proved it never received it and the idempotency window is open (-> PENDING, same key).
        Neither: still unproven (-> UNKNOWN, retried later, `reconcile_attempts` + 1)."""
        ...

    async def get(self, tenant_id: str, outbox_id: str) -> OutboundMessage | None: ...
