from __future__ import annotations

from typing import Protocol

from conversation_agent.core.models.runtime import ConversationKey, InboundEvent


class InboxStore(Protocol):
    async def insert_if_absent(self, event: InboundEvent) -> bool:
        """Persist before acknowledging. False if (tenant, channel, event_id) already existed
        (INV-020): a duplicate never creates a second turn."""
        ...

    async def withdraw_unprocessed(
        self, tenant_id: str, channel_id: str, provider_message_id: str
    ) -> int:
        """The contact deleted a message: events carrying that provider id that no turn has
        claimed yet are dropped (DEAD), so a deleted "yes" can never be processed. Returns how
        many were withdrawn; a turn already in flight is not interrupted."""
        ...

    async def list_ready_conversations(self, limit: int = 50) -> list[ConversationKey]:
        """Candidate selection only: it never changes event ownership. Events are claimed
        later, inside the conversation lease (RUNTIME_PROTOCOL §2)."""
        ...
