"""Resolves outbox rows whose delivery is UNKNOWN by asking the channel (DESIGN 26.1).

channel knows the message        -> record what it says (ACCEPTED / QUEUED / FAILED)
channel PROVES it has none       -> resend with the SAME key, but only while the channel's
                                    idempotency memory is open (otherwise "absent" proves
                                    nothing: it may simply have forgotten the key)
anything else (error, too old)   -> stays UNKNOWN, retried later; never a blind resend
"""

from __future__ import annotations

import logging
from datetime import timedelta

from conversation_agent.core.models.delivery import DeliveryPolicy
from conversation_agent.core.models.runtime import OutboundMessage
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
    ) -> None:
        self._outbox = outbox
        self._channel = channel
        self._coordination = coordination
        self._policy = policy
        self._owner = owner
        self._claim_ttl = claim_ttl
        self._retry_after = retry_after

    async def run_once(self, limit: int = 20) -> int:
        messages = await self._outbox.claim_unknown(self._owner, limit, self._claim_ttl)
        for message in messages:
            await self._reconcile(message)
        return len(messages)

    async def _reconcile(self, message: OutboundMessage) -> None:
        try:
            found = await self._channel.lookup(message)
        except Exception:
            # the question could not be answered: that proves nothing
            await self._outbox.record_reconciliation(
                message,
                self._owner,
                found=None,
                resend=False,
                retry_after=self._retry_after,
                note="LOOKUP_FAILED",
            )
            return
        if found is not None:
            await self._outbox.record_reconciliation(
                message, self._owner, found=found, resend=False, retry_after=self._retry_after
            )
            return
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
            "ALERT outbox %s is UNKNOWN past the idempotency window and the channel has no record: "
            "not resending (a person or a new prompt must decide)",
            message.outbox_id,
        )
        await self._outbox.record_reconciliation(
            message,
            self._owner,
            found=None,
            resend=False,
            retry_after=self._retry_after,
            note="UNPROVEN_PAST_RETENTION",
        )
