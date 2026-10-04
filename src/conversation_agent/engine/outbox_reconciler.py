"""Settles outbox rows whose outcome the channel has not reported (DESIGN 26.1).

The gateway's contract (docs/RELAYPLANE_CONTRACT.md) gives two facts this relies on:

  - the SAME Idempotency-Key is replayed, not duplicated, for as long as the gateway remembers
    it (its idempotency retention). A resend inside that window is therefore SAFE even when we
    do not know whether the first attempt arrived;
  - once the gateway answered with its own message id, `lookup` tells the truth about it.

    row with the gateway's id      -> ask: ACCEPTED / QUEUED / FAILED are recorded; the gateway's
                                      own UNKNOWN waits for a decision (never guessed)
    row without it, inside window  -> resend the same key (PENDING): a replay, never a duplicate
    row without it, past window    -> UNPROVEN: the key may be forgotten, a resend could duplicate;
                                      it stays UNKNOWN for a person/new prompt (alert)
    lookup failed                  -> stays UNKNOWN, retried later
"""

from __future__ import annotations

import logging
from datetime import timedelta

from conversation_agent.core.models.delivery import DeliveryPolicy
from conversation_agent.core.models.runtime import OutboundMessage, OutboxStatus
from conversation_agent.ports.channel_lookup import MessageLookup
from conversation_agent.ports.coordination import CoordinationClock
from conversation_agent.ports.outbox import OutboxStore

log = logging.getLogger(__name__)


class OutboxReconciler:
    def __init__(
        self,
        outbox: OutboxStore,
        channel: MessageLookup,
        coordination: CoordinationClock,
        policy: DeliveryPolicy,
        *,
        owner: str,
        claim_ttl: timedelta = timedelta(seconds=30),
        retry_after: timedelta = timedelta(seconds=30),
        poll_after: timedelta = timedelta(seconds=30),
    ) -> None:
        self._outbox = outbox
        self._channel = channel
        self._coordination = coordination
        self._policy = policy
        self._owner = owner
        self._claim_ttl = claim_ttl
        self._retry_after = retry_after
        self._poll_after = poll_after

    async def run_once(self, limit: int = 20) -> int:
        messages = await self._outbox.claim_unsettled(
            self._owner, limit, self._claim_ttl, self._poll_after
        )
        for message in messages:
            await self._reconcile(message)
        return len(messages)

    async def _unproven(self, message: OutboundMessage, note: str) -> None:
        await self._outbox.record_reconciliation(
            message, self._owner, found=None, resend=False, retry_after=self._retry_after, note=note
        )

    async def _reconcile(self, message: OutboundMessage) -> None:
        if message.channel_message_id is None:
            age = (
                (await self._coordination.now()) - message.first_sent_at
                if message.first_sent_at is not None
                else timedelta(0)
            )
            if age <= self._policy.safe_resend_until:
                await self._outbox.record_reconciliation(
                    message, self._owner, found=None, resend=True, retry_after=self._retry_after
                )
                return
            log.error(
                "ALERT outbox %s has no gateway id and is past the idempotency window: not "
                "resending (the key may be forgotten and the message duplicated)",
                message.outbox_id,
            )
            await self._unproven(message, "UNPROVEN_PAST_RETENTION")
            return
        try:
            found = await self._channel.lookup(message)
        except Exception:
            await self._unproven(message, "LOOKUP_FAILED")
            return
        if found.status is OutboxStatus.UNKNOWN:
            log.error(
                "ALERT outbox %s is UNKNOWN at the gateway: it needs a decision (resolve)",
                message.outbox_id,
            )
        await self._outbox.record_reconciliation(
            message,
            self._owner,
            found=found,
            resend=False,
            retry_after=self._retry_after,
            note="CHANNEL_UNKNOWN_NEEDS_DECISION" if found.status is OutboxStatus.UNKNOWN else None,
        )
