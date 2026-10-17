from __future__ import annotations

from typing import Protocol, runtime_checkable

from conversation_agent.core.models.runtime import OutboundMessage, SendResult


@runtime_checkable
class MessageLookup(Protocol):
    """Asks the channel what became of a send it already acknowledged (it gave us its id).

    Optional: a channel may instead settle QUEUED/UNKNOWN sends by pushing status through
    `OutboxStore.apply_channel_status`. Either path (or both) must exist for a channel that can
    answer non-terminal; `Runtime.build` detects a sender that satisfies this protocol."""

    async def lookup(self, message: OutboundMessage) -> SendResult:
        """The channel's current answer for `message.channel_message_id`. Raises when the question
        could not be answered: that proves nothing."""
        ...
