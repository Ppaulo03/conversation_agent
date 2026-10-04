from __future__ import annotations

from datetime import datetime, time, timedelta

from conversation_agent.core.models.conversation import ConversationIdentity
from conversation_agent.ports.channel_policy import ChannelDecision


class ServiceWindowPolicy:
    """Proactive messages only inside a window after the contact last wrote (and, optionally,
    outside quiet hours). The window length is configuration of THIS adapter: the core hard-codes
    no channel rule."""

    def __init__(
        self,
        window: timedelta = timedelta(hours=24),
        *,
        quiet_hours: tuple[time, time] | None = None,
    ) -> None:
        self._window = window
        self._quiet = quiet_hours

    async def evaluate(
        self,
        identity: ConversationIdentity,
        *,
        now: datetime,
        last_contact_event_at: datetime | None,
        kind: str,
    ) -> ChannelDecision:
        if last_contact_event_at is None:
            return ChannelDecision(allowed=False, reason="NEVER_CONTACTED")
        if now - last_contact_event_at > self._window:
            return ChannelDecision(allowed=False, reason="OUTSIDE_SERVICE_WINDOW")
        if self._quiet is not None:
            start, end = self._quiet
            current = now.timetz().replace(tzinfo=None)
            inside = (
                (start <= current < end) if start <= end else (current >= start or current < end)
            )
            if inside:
                return ChannelDecision(allowed=False, reason="QUIET_HOURS")
        return ChannelDecision(allowed=True, reason="ALLOWED")
