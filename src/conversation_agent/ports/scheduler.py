from __future__ import annotations

from datetime import timedelta
from typing import Protocol

from conversation_agent.core.models.runtime import ScheduledEvent


class Scheduler(Protocol):
    """Durable timers: they survive restarts (never `asyncio.sleep` in a worker)."""

    async def schedule(self, event: ScheduledEvent) -> None:
        """Upsert by (tenant_id, scheduler_key): rescheduling replaces a pending timer."""
        ...

    async def cancel(self, tenant_id: str, scheduler_key: str) -> None: ...

    async def claim_due(
        self, owner: str, limit: int, claim_ttl: timedelta
    ) -> list[ScheduledEvent]: ...

    async def complete(self, tenant_id: str, scheduler_key: str, owner: str) -> None: ...
