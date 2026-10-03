from __future__ import annotations

from typing import Protocol

from conversation_agent.core.models.runtime import ConversationKey, InboundEvent


class InboxStore(Protocol):
    async def insert_if_absent(self, event: InboundEvent) -> bool:
        """Persist before acknowledging. False if (tenant, channel, event_id) already existed
        (INV-020): a duplicate never creates a second turn."""
        ...

    async def list_ready_conversations(self, limit: int = 50) -> list[ConversationKey]:
        """Candidate selection only: it never changes event ownership. Events are claimed
        later, inside the conversation lease (RUNTIME_PROTOCOL §2)."""
        ...
