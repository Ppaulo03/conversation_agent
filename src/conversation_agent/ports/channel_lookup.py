from __future__ import annotations

from typing import Protocol

from conversation_agent.core.models.runtime import OutboundMessage, SendResult


class MessageLookup(Protocol):
    """Asks the channel what became of a message, by its idempotency key (reconciliation)."""

    async def lookup(self, message: OutboundMessage) -> SendResult | None:
        """The channel's answer, or None when it PROVES it has no such message. Raises when the
        question itself could not be answered (that proves nothing)."""
        ...
