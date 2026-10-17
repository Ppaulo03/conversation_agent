from __future__ import annotations

from typing import Protocol

from conversation_agent.core.models.runtime import OutboundMessage, SendResult


class MessageSender(Protocol):
    """Delivers one persisted outbox row to the channel (contract: ADR-023, RUNTIME_PROTOCOL §8).

    Idempotency: one logical delivery keeps the same `idempotency_key` and the same payload on every
    resend (after a crash, a retry or a reconciler-authorised resend); a new key is a new delivery.
    The channel must dedupe on that key for at least the period the Runtime's `DeliveryPolicy`
    relies on (`idempotency_retention`); a channel that cannot promise it needs
    `retry_horizon == 0`.

    What `send` returns:
      ACCEPTED  the channel accepted it: terminal, not necessarily delivered or read.
      QUEUED    acknowledged, settlement pending: NON-terminal and MUST carry `channel_message_id`.
      UNKNOWN   the effect cannot be proven (it may or may not have happened).
      FAILED    the channel PROVES the attempt did not succeed; `retryable` (proven FAILED only)
                says whether the same key may be tried again.
    Known definitive or pre-send failures must be returned as FAILED. An exception escaping `send`
    is treated by the framework as UNKNOWN (it cannot tell whether the channel saw the request).

    A sender that can produce QUEUED or UNKNOWN needs a viable settlement path: `MessageLookup`
    (implemented by the sender or passed to `Runtime.build`), status pushed through
    `OutboxStore.apply_channel_status`, or both."""

    async def send(self, message: OutboundMessage) -> SendResult: ...
