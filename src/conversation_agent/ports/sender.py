from __future__ import annotations

from typing import Protocol

from conversation_agent.core.models.runtime import OutboundMessage, SendResult


class MessageSender(Protocol):
    """Delivers one persisted outbox row to the channel. Must dedupe on
    `message.idempotency_key` (the same payload may be sent again after a crash)."""

    async def send(self, message: OutboundMessage) -> SendResult: ...
