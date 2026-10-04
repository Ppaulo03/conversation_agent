from __future__ import annotations

from typing import Protocol

from conversation_agent.core.models.runtime import OutboundMessage, SendResult


class MessageLookup(Protocol):
    """Asks the channel what became of a send it already acknowledged (it gave us its id)."""

    async def lookup(self, message: OutboundMessage) -> SendResult:
        """The channel's current answer for `message.channel_message_id`. Raises when the question
        could not be answered: that proves nothing."""
        ...
