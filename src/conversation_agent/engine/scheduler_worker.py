"""Dispatches due durable timers to handlers. A handler that fails (or a worker that dies)
leaves the event CLAIMED; it becomes claimable again once its claim expires."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta

from conversation_agent.core.models.runtime import ScheduledEvent
from conversation_agent.ports.scheduler import Scheduler

Handler = Callable[[ScheduledEvent], Awaitable[None]]


class SchedulerWorker:
    def __init__(
        self,
        scheduler: Scheduler,
        handlers: dict[str, Handler],
        *,
        owner: str,
        claim_ttl: timedelta = timedelta(seconds=30),
    ) -> None:
        self._scheduler = scheduler
        self._handlers = handlers
        self._owner = owner
        self._ttl = claim_ttl

    async def run_once(self, limit: int = 20) -> int:
        events = await self._scheduler.claim_due(self._owner, limit, self._ttl)
        handled = 0
        for event in events:
            handler = self._handlers.get(event.event_type)
            if handler is None:
                continue  # unknown type: stays claimed, surfaces as a stuck timer
            await handler(event)
            await self._scheduler.complete(event.tenant_id, event.scheduler_key, self._owner)
            handled += 1
        return handled
