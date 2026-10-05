"""Outbox sender worker: the only path to the channel (INV-007).

Rows were persisted atomically with the turn that produced them; this worker claims them,
hands them to the MessageSender and records the outcome. A crash anywhere in between leaves
the row SENDING; it is later retried with the same payload and idempotency key - but only while
the channel still remembers that key (INV-034): past `retry_horizon` the row is handed to the
reconciler instead of being re-sent blindly.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from conversation_agent.core.models.runtime import OutboundMessage, OutboxStatus, SendResult
from conversation_agent.core.observability import bind
from conversation_agent.core.tracing import span
from conversation_agent.ports.coordination import CoordinationClock
from conversation_agent.ports.faults import FaultInjector
from conversation_agent.ports.outbox import OutboxStore
from conversation_agent.ports.sender import MessageSender

log = logging.getLogger(__name__)


MAX_PASSES = 8  # back-to-back claims per run (a reply has at most a handful of parts)


class OutboxWorker:
    def __init__(
        self,
        outbox: OutboxStore,
        sender: MessageSender,
        faults: FaultInjector,
        *,
        owner: str,
        claim_ttl: timedelta = timedelta(seconds=30),
        retry_after: timedelta = timedelta(seconds=5),
        retry_horizon: timedelta | None = None,
        coordination: CoordinationClock | None = None,
        scope: str | None = None,
        unknown_blocks_for: timedelta = timedelta(hours=24),
    ) -> None:
        self._scope = scope  # only messages of this scope's conversations are claimed
        self._unknown_blocks_for = (
            unknown_blocks_for  # how long an unknown send holds back the next
        )
        if retry_horizon is not None and coordination is None:
            raise ValueError("a retry horizon needs the coordination clock to measure message age")
        self._outbox = outbox
        self._sender = sender
        self._faults = faults
        self._owner = owner
        self._claim_ttl = claim_ttl
        self._retry_after = retry_after
        self._horizon = retry_horizon
        self._coordination = coordination

    async def run_once(self, limit: int = 20) -> int:
        """Claim and send what is ready. A conversation yields ONE message per claim (the next
        waits for it), so several passes run back to back: the parts of one reply leave together."""
        total = 0
        for _ in range(MAX_PASSES):
            messages = await self._outbox.claim_ready(
                self._owner, limit, self._claim_ttl, self._scope, self._unknown_blocks_for
            )
            if not messages:
                break
            for message in messages:
                with (
                    bind(
                        tenant_id=message.tenant_id,
                        outbox_id=message.outbox_id,
                        trace_id=message.trace_id,
                        component="outbox_worker",
                    ),
                    span("outbox.send", attempts=message.attempts),
                ):
                    await self._deliver(message)
            total += len(messages)
        return total

    async def _deliver(self, message: OutboundMessage) -> None:
        if await self._past_the_horizon(message):
            # Another attempt may already have reached the channel and its dedupe memory
            # may be fading: ask, do not resend (reconciliation decides).
            log.warning(
                "outbox.past_horizon_not_resent", extra={"fields": {"attempts": message.attempts}}
            )
            await self._outbox.record_result(
                message, self._owner, SendResult(status=OutboxStatus.UNKNOWN), self._retry_after
            )
            return
        try:
            result = await self._sender.send(message)
        except Exception as exc:
            # The request may have reached the channel: unknown, never "failed, resend".
            log.warning("outbox.send_raised", extra={"fields": {"error": type(exc).__name__}})
            result = SendResult(status=OutboxStatus.UNKNOWN)
        await self._faults.hit("C08_during_outbox_send")  # sent, not yet recorded
        if result.status in (OutboxStatus.UNKNOWN, OutboxStatus.FAILED):
            log.warning(
                "outbox.send_not_confirmed",
                extra={
                    "fields": {
                        "status": result.status.value,
                        "retryable": result.retryable,
                        "attempts": message.attempts,
                    }
                },
            )
        await self._outbox.record_result(message, self._owner, result, self._retry_after)

    async def _past_the_horizon(self, message: OutboundMessage) -> bool:
        if message.resend_authorized_until is not None and self._coordination is not None:
            # The reconciler proved the channel had no such message, and allowed a resend UNTIL a
            # deadline. That was then; a worker that wakes up after it must not send (INV-021).
            return (await self._coordination.now()) > message.resend_authorized_until
        if self._horizon is None or self._coordination is None:
            return False
        if message.attempts <= 1 or message.first_sent_at is None:
            return False  # the first send is always allowed
        age = (await self._coordination.now()) - message.first_sent_at
        return age > self._horizon
