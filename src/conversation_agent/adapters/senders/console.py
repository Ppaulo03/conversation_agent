"""The terminal as a channel: replies are printed, what the person types becomes inbound events.

It is a real (if tiny) channel, so it provides what a confirmation needs from one (DESIGN §10):
the SENDER dedupes on the idempotency key and reports when it accepted each message, and the
CHANNEL stamps every inbound event with its own time and with the message it answers, so "yes" is
provably a reply to the prompt that is on screen. Dedupe memory lives in the process: a restart
of a local session may print a repeated message again (a real provider keeps it for you).
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Callable

from conversation_agent.core.models.conversation import ConversationIdentity
from conversation_agent.core.models.runtime import (
    InboundEvent,
    OutboundMessage,
    OutboxStatus,
    SendResult,
)
from conversation_agent.ports.clock import Clock


class ConsoleChannel:
    def __init__(
        self,
        identity: ConversationIdentity,
        clock: Clock,
        *,
        write: Callable[[str], None] = print,
        prefix: str = "bot> ",
    ) -> None:
        self._identity = identity
        self._clock = clock
        self._write = write
        self._prefix = prefix
        self._by_key: dict[str, SendResult] = {}
        self._ids = itertools.count(1)
        self._run = uuid.uuid4().hex[:8]  # ids stay unique across restarts of a local session
        self._sequence = itertools.count(1)
        self._last_shown: str | None = None
        self.delivered: list[OutboundMessage] = []

    async def send(self, message: OutboundMessage) -> SendResult:
        """MessageSender: show the text once per idempotency key and say when it was accepted."""
        known = self._by_key.get(message.idempotency_key)
        if known is not None:
            return known
        provider_id = f"console-{self._run}-{next(self._ids)}"
        result = SendResult(
            status=OutboxStatus.ACCEPTED,
            provider_message_id=provider_id,
            provider_accepted_at=self._clock.now(),
        )
        self._by_key[message.idempotency_key] = result
        self._last_shown = provider_id
        self.delivered.append(message)
        self._write(f"{self._prefix}{message.text}")
        return result

    def inbound(self, text: str) -> InboundEvent:
        """What the person typed, as the channel reports it: its own clock and the message it
        was answering (the last thing shown)."""
        now = self._clock.now()
        sequence = next(self._sequence)
        return InboundEvent(
            tenant_id=self._identity.tenant_id,
            channel_id=self._identity.channel_id,
            event_id=f"console-{self._run}-{sequence}",
            conversation_id=self._identity.conversation_id,
            contact_id=self._identity.contact_id,
            session_id=self._identity.session_id,
            text=text,
            occurred_at=now,
            received_at=now,
            provider_occurred_at=now,
            reply_to_provider_message_id=self._last_shown,
        )
