"""Settles outbox rows whose outcome the channel has not reported (DESIGN 26.1, ADR-023).

Channel-generic. What it relies on is stated by the DeliveryPolicy, never assumed:

  - the SAME idempotency key is replayed, not duplicated, for as long as the channel remembers it
    (`idempotency_retention`). A resend inside the policy's safe resend window is therefore SAFE
    even when we do not know whether the first attempt arrived;
  - once the channel gave a handle (`channel_message_id`), a `MessageLookup` tells the truth about
    it. Lookup is OPTIONAL: a channel may settle by pushing status through
    `OutboxStore.apply_channel_status` instead (or as well).

    row with a handle, lookup configured  -> ask: ACCEPTED / QUEUED / FAILED are recorded; the
                                              channel's own UNKNOWN waits for a decision (never
                                              guessed)
    row with a handle, no lookup           -> not touched: it waits for the pushed status, visible
                                              as unsettled (QUEUED never goes to a blind resend)
    row without it, retries enabled,
      inside the safe resend window        -> resend the same key (PENDING): a replay, not a dup
    row without it, no retries (horizon 0) -> stays UNKNOWN (observable): it is never sent again
    row without it, past the window        -> UNPROVEN: the key may be forgotten, a resend could
                                              duplicate; it stays UNKNOWN (alert)
    lookup failed                          -> stays UNKNOWN, retried later
"""

from __future__ import annotations

import logging
from datetime import timedelta

from conversation_agent.core.models.delivery import DeliveryPolicy
from conversation_agent.core.models.runtime import OutboundMessage, OutboxStatus
from conversation_agent.core.observability import bind
from conversation_agent.core.tracing import span
from conversation_agent.ports.channel_lookup import MessageLookup
from conversation_agent.ports.coordination import CoordinationClock
from conversation_agent.ports.outbox import OutboxStore

log = logging.getLogger(__name__)


class OutboxReconciler:
    def __init__(
        self,
        outbox: OutboxStore,
        lookup: MessageLookup | None,
        coordination: CoordinationClock,
        policy: DeliveryPolicy,
        *,
        owner: str,
        claim_ttl: timedelta = timedelta(seconds=30),
        retry_after: timedelta = timedelta(seconds=30),
        poll_after: timedelta = timedelta(seconds=30),
        scope: str | None = None,
    ) -> None:
        self._scope = scope  # only messages of this scope's conversations are reconciled
        self._outbox = outbox
        self._lookup = lookup
        self._coordination = coordination
        self._policy = policy
        self._owner = owner
        self._claim_ttl = claim_ttl
        self._retry_after = retry_after
        self._poll_after = poll_after

    async def run_once(self, limit: int = 20) -> int:
        messages = await self._outbox.claim_unsettled(
            self._owner,
            limit,
            self._claim_ttl,
            self._poll_after,
            self._scope,
            with_handle=self._lookup is not None,
        )
        for message in messages:
            await self._reconcile(message)
        return len(messages)

    async def _unproven(self, message: OutboundMessage, note: str) -> None:
        await self._outbox.record_reconciliation(
            message, self._owner, found=None, resend=False, retry_after=self._retry_after, note=note
        )

    async def _reconcile(self, message: OutboundMessage) -> None:
        with (
            bind(
                tenant_id=message.tenant_id,
                outbox_id=message.outbox_id,
                trace_id=message.trace_id,
                component="outbox_reconciler",
            ),
            span("outbox.reconcile"),
        ):
            await self._reconcile_one(message)

    async def _reconcile_one(self, message: OutboundMessage) -> None:
        if message.channel_message_id is None:
            age = (
                (await self._coordination.now()) - message.first_sent_at
                if message.first_sent_at is not None
                else timedelta(0)
            )
            if not self._policy.retries_enabled:
                # retry_horizon == 0: this delivery is never sent again; it stays observable
                await self._unproven(message, "RESEND_NOT_PERMITTED")
                return
            if age <= self._policy.safe_resend_window:
                now = await self._coordination.now()
                # the authorisation expires when the channel may forget the key, not when a worker
                # gets around to it
                until = (message.first_sent_at or now) + self._policy.safe_resend_window
                await self._outbox.record_reconciliation(
                    message,
                    self._owner,
                    found=None,
                    resend=True,
                    retry_after=self._retry_after,
                    resend_until=until,
                )
                return
            log.error(
                "outbox.unproven_past_retention",
                extra={"fields": {"alert": True, "attempts": message.attempts}},
            )  # no handle and past the idempotency window: not resending (it could duplicate)
            await self._unproven(message, "UNPROVEN_PAST_RETENTION")
            return
        if self._lookup is None:
            return  # unreachable by the claim; a handle without a lookup waits for the push
        try:
            found = await self._lookup.lookup(message)
        except Exception:
            await self._unproven(message, "LOOKUP_FAILED")
            return
        if found.status is OutboxStatus.UNKNOWN:
            log.error(
                "outbox.channel_unknown_needs_decision", extra={"fields": {"alert": True}}
            )  # the channel itself is unsure: its own resolution mechanism decides
        await self._outbox.record_reconciliation(
            message,
            self._owner,
            found=found,
            resend=False,
            retry_after=self._retry_after,
            note="CHANNEL_UNKNOWN_NEEDS_DECISION" if found.status is OutboxStatus.UNKNOWN else None,
        )
