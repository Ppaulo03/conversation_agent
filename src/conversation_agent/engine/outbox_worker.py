"""Outbox sender worker: the only path to the channel (INV-007).

Rows were persisted atomically with the turn that produced them; this worker claims them,
hands them to the MessageSender and records the outcome. A crash anywhere in between leaves
the row SENDING; it is later retried with the same payload and idempotency key.
"""

from __future__ import annotations

from datetime import timedelta

from conversation_agent.core.models.runtime import OutboxStatus, SendResult
from conversation_agent.ports.faults import FaultInjector
from conversation_agent.ports.outbox import OutboxStore
from conversation_agent.ports.sender import MessageSender


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
    ) -> None:
        self._outbox = outbox
        self._sender = sender
        self._faults = faults
        self._owner = owner
        self._claim_ttl = claim_ttl
        self._retry_after = retry_after

    async def run_once(self, limit: int = 20) -> int:
        messages = await self._outbox.claim_ready(self._owner, limit, self._claim_ttl)
        for message in messages:
            try:
                result = await self._sender.send(message)
            except Exception:
                # The request may have reached the channel: unknown, never "failed, resend".
                result = SendResult(status=OutboxStatus.UNKNOWN)
            await self._faults.hit("C08_during_outbox_send")  # sent, not yet recorded
            await self._outbox.record_result(message, self._owner, result, self._retry_after)
        return len(messages)
