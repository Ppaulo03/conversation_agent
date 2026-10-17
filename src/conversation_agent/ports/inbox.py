from __future__ import annotations

from datetime import timedelta
from typing import Protocol

from conversation_agent.core.models.runtime import ConversationKey, InboundEvent


class InboxStore(Protocol):
    async def insert_if_absent(self, event: InboundEvent) -> bool:
        """Persist before acknowledging. True: a new logical event was stored. False: its
        `(tenant, channel, event_id)` already existed (INV-020): acknowledge it as a success; a
        duplicate creates no turn and no new conversation state, the first writer stays
        authoritative and a differing payload is not compared.

        A new event that reuses a `conversation_id` of this tenant with another channel, contact
        or scope raises `ConversationIdentityConflictError` and is not stored. Any other failure
        leaves durable acceptance unconfirmed: do not acknowledge a redeliverable transport and
        retry the SAME event (same `event_id`); if the first attempt committed, the retry is a
        duplicate (False), otherwise it is stored."""
        ...

    async def withdraw_unprocessed(
        self, tenant_id: str, channel_id: str, provider_message_id: str
    ) -> int:
        """The contact deleted a message: events carrying that provider id that no turn has
        claimed yet are dropped (DEAD), so a deleted "yes" can never be processed. Returns how
        many were withdrawn; a turn already in flight is not interrupted."""
        ...

    async def list_ready_conversations(
        self,
        limit: int = 50,
        scope: str | None = None,
        quiet: timedelta | None = None,
        max_wait: timedelta | None = None,
    ) -> list[ConversationKey]:
        """Candidate selection only: it never changes event ownership. Events are claimed
        later, inside the conversation lease (RUNTIME_PROTOCOL §2)."""
        ...
