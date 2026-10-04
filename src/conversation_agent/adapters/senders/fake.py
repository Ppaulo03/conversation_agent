from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from conversation_agent.core.models.runtime import OutboundMessage, OutboxStatus, SendResult


class FakeMessageSender:
    """A channel that dedupes on the idempotency key, like a real provider would."""

    def __init__(
        self,
        *,
        result: SendResult | None = None,
        provider_clock: Callable[[], datetime] | None = None,
    ) -> None:
        """`provider_clock` simulates the CHANNEL's clock for `provider_accepted_at`; without
        it the channel reports no timestamp (like a provider that does not expose one)."""
        self._provider_clock = provider_clock
        self._by_key: dict[str, SendResult] = {}
        self.delivered: list[OutboundMessage] = []  # actual channel deliveries (deduped)
        self.attempts: list[OutboundMessage] = []  # every send() call, retries included
        self.next_result = result
        self.fail_next: Exception | None = None

    async def send(self, message: OutboundMessage) -> SendResult:
        self.attempts.append(message)
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        if message.idempotency_key in self._by_key:
            return self._by_key[message.idempotency_key]
        result = self.next_result or SendResult(
            status=OutboxStatus.ACCEPTED,
            provider_message_id=f"prov-{len(self.delivered) + 1}",
            provider_accepted_at=self._provider_clock() if self._provider_clock else None,
        )
        self._by_key[message.idempotency_key] = result
        self.delivered.append(message)
        return result
