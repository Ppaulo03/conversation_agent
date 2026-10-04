"""Proactive messages (DESIGN 28): timers re-enter through the SAME runtime.

    ScheduledEvent("proactive.message")  -- durable timer (survives restarts)
      -> ProactiveEventHandler: a SYSTEM inbound event, deduped by the timer's key
      -> normal turn machinery: lease, ownership gate (HUMAN stays silent), ChannelPolicy,
         Outbox (the only way out)

The handler never sends and never decides: it only turns "time passed" into an inbound event.
"""

from __future__ import annotations

from datetime import datetime

from conversation_agent.core.canonical import stable_hash
from conversation_agent.core.models.conversation import ConversationIdentity
from conversation_agent.core.models.runtime import InboundEvent, ScheduledEvent
from conversation_agent.ports.clock import Clock
from conversation_agent.ports.inbox import InboxStore

PROACTIVE_EVENT = "proactive.message"


def proactive_timer(
    identity: ConversationIdentity,
    *,
    text: str,
    due_at: datetime,
    reason: str,
    key: str | None = None,
) -> ScheduledEvent:
    """A durable timer; its key is deterministic, so arming it twice is one timer."""
    scheduler_key = (
        key or f"proactive:{stable_hash(identity.conversation_id, reason, due_at.isoformat())[:24]}"
    )
    return ScheduledEvent(
        tenant_id=identity.tenant_id,
        scheduler_key=scheduler_key,
        event_type=PROACTIVE_EVENT,
        due_at=due_at,
        payload={
            "channel_id": identity.channel_id,
            "conversation_id": identity.conversation_id,
            "contact_id": identity.contact_id,
            "session_id": identity.session_id,
            "text": text,
            "reason": reason,
        },
    )


class ProactiveEventHandler:
    def __init__(self, inbox: InboxStore, clock: Clock) -> None:
        self._inbox = inbox
        self._clock = clock

    async def __call__(self, timer: ScheduledEvent) -> None:
        p = timer.payload
        now = self._clock.now()
        await self._inbox.insert_if_absent(
            InboundEvent(
                tenant_id=timer.tenant_id,
                channel_id=p["channel_id"],
                event_id=f"system:{timer.scheduler_key}",  # a re-fired timer is the same event
                conversation_id=p["conversation_id"],
                contact_id=p["contact_id"],
                session_id=p["session_id"],
                text=p["text"],
                occurred_at=now,
                received_at=now,
                kind="system",
            )
        )
