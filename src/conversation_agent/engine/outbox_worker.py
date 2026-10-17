"""Outbox sender worker: the only path to the channel (INV-007).

Rows were persisted atomically with the turn that produced them; this worker claims them,
hands them to the MessageSender and records the outcome. A crash anywhere in between leaves
the row SENDING; it may be sent again with the same payload and idempotency key, but only as
the mandatory DeliveryPolicy allows (INV-034, INV-072, ADR-023):

  - never outside the safe resend window (the channel may have forgotten the key);
  - never when `retry_horizon == 0`;
  - a retry of a FAILED the channel proved needs the horizon; once it closes (or the attempt cap
    is hit) the row is terminally FAILED, never UNKNOWN: nothing about it is ambiguous;
  - a stale SENDING is ambiguous; if its window closed the row becomes UNKNOWN, not FAILED, and
    the reconciler decides (it can still ask the channel, or authorise one same-key resend).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from conversation_agent.core.models.delivery import DeliveryPolicy
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
        policy: DeliveryPolicy,
        coordination: CoordinationClock,
        claim_ttl: timedelta = timedelta(seconds=30),
        retry_after: timedelta = timedelta(seconds=5),
        scope: str | None = None,
    ) -> None:
        self._scope = scope  # only messages of this scope's conversations are claimed
        self._outbox = outbox
        self._sender = sender
        self._faults = faults
        self._owner = owner
        self._policy = policy
        self._claim_ttl = claim_ttl
        self._retry_after = retry_after
        self._coordination = coordination

    async def run_once(self, limit: int = 20) -> int:
        """Claim and send what is ready. A conversation yields ONE message per claim (the next
        waits for it), so several passes run back to back: the parts of one reply leave together."""
        total = 0
        for _ in range(MAX_PASSES):
            messages = await self._outbox.claim_ready(
                self._owner,
                limit,
                self._claim_ttl,
                self._scope,
                unknown_blocks_for=self._policy.safe_resend_window,
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
        refused = await self._resend_refused(message)
        if refused is not None:
            log.warning(
                "outbox.resend_refused",
                extra={"fields": {"attempts": message.attempts, "outcome": refused.status.value}},
            )
            await self._outbox.record_result(message, self._owner, refused, self._retry_after)
            return
        try:
            result = await self._sender.send(message)
        except Exception as exc:
            # The request may have reached the channel: unknown, never "failed, resend".
            log.warning("outbox.send_raised", extra={"fields": {"error": type(exc).__name__}})
            result = SendResult(status=OutboxStatus.UNKNOWN)
        await self._faults.hit("C08_during_outbox_send")  # sent, not yet recorded
        if (
            result.status is OutboxStatus.FAILED
            and result.retryable
            and not await self._may_retry_after(message)
        ):
            result = result.model_copy(update={"retryable": False})  # terminal: a proven FAILED
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

    async def _may_retry_after(self, message: OutboundMessage) -> bool:
        """Whether the next attempt of a proven-FAILED row (due in `retry_after`) still fits the
        retry horizon. The attempt cap is the store's."""
        if not self._policy.retries_enabled or message.first_sent_at is None:
            return False
        age = (await self._coordination.now()) - message.first_sent_at + self._retry_after
        return age <= self._policy.retry_horizon

    async def _resend_refused(self, message: OutboundMessage) -> SendResult | None:
        """The result to record INSTEAD of sending, or None when the send may go ahead.

        The first attempt is always allowed. A later one is a resend of the same key and must pass
        the policy; what a refusal means depends on what we know about the earlier attempt:
        a FAILED the channel proved -> terminal FAILED; an ambiguous one -> UNKNOWN."""
        if message.attempts <= 1 or message.first_sent_at is None:
            return None
        ambiguous = SendResult(status=OutboxStatus.UNKNOWN)
        proven = SendResult(status=OutboxStatus.FAILED, retryable=False)
        now = await self._coordination.now()
        age = now - message.first_sent_at
        policy = self._policy
        # A claim from PENDING without a reconciler authorisation is the retry of a proven FAILED;
        # a stale SENDING (or anything we cannot classify) is ambiguous.
        authorised = message.resend_authorized_until is not None
        proven_retry = message.claimed_from is OutboxStatus.PENDING and not authorised
        refusal = proven if proven_retry else ambiguous
        if not policy.retries_enabled or age > policy.safe_resend_window:
            return refusal
        if message.claimed_from is OutboxStatus.PENDING and authorised:
            # the reconciler proved the channel had no such message, and allowed a resend UNTIL a
            # deadline. That was then; a worker that wakes up after it must not send (INV-021).
            assert message.resend_authorized_until is not None
            return None if now <= message.resend_authorized_until else ambiguous
        return None if age <= policy.retry_horizon else refusal
