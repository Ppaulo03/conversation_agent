from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from conversation_agent.core.models.conversation import ConversationIdentity


class ChannelDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    reason: str  # stable code, journaled with the decision


class ChannelPolicy(Protocol):
    """Channel rules for messages the CONTACT did not just ask for (service windows, quiet
    hours, rate limits). They belong to the channel, not to the core: the framework only asks."""

    async def evaluate(
        self,
        identity: ConversationIdentity,
        *,
        now: datetime,
        last_contact_event_at: datetime | None,
        kind: str,
    ) -> ChannelDecision: ...
