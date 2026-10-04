from __future__ import annotations

from collections.abc import Iterable

from conversation_agent.ports.subscriptions import Subscription


class StaticSubscriptionResolver:
    """Operator-registered webhook endpoints, held in process."""

    def __init__(self, subscriptions: Iterable[Subscription]) -> None:
        self._by_id = {s.subscription_id: s for s in subscriptions}

    async def resolve(self, subscription_id: str) -> Subscription | None:
        return self._by_id.get(subscription_id)
