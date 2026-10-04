"""Conversation-lease heartbeat (RUNTIME_PROTOCOL §3).

A background compare-and-set renews the lease on (owner, epoch) at an interval shorter than
the TTL. If a renewal fails the worker is *stale*: it starts no new step and stops at the next
safe boundary; external I/O already in flight only records its result via execution_epoch.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta

from conversation_agent.core.errors import StaleWorkerError
from conversation_agent.core.models.runtime import FenceToken, Lease
from conversation_agent.ports.clock import Clock
from conversation_agent.ports.lease import ConversationLeaseStore


class LeaseHandle:
    def __init__(
        self,
        lease: Lease,
        store: ConversationLeaseStore,
        clock: Clock,
        *,
        ttl: timedelta,
        interval_seconds: float,
    ) -> None:
        if interval_seconds >= ttl.total_seconds():
            raise ValueError("heartbeat interval must be shorter than the lease TTL")
        self._lease = lease
        self._store = store
        self._clock = clock
        self._ttl = ttl
        self._interval = interval_seconds
        self._stale = False
        self._task: asyncio.Task[None] | None = None

    @property
    def lease(self) -> Lease:
        return self._lease

    @property
    def fence(self) -> FenceToken:
        return self._lease.fence

    @property
    def stale(self) -> bool:
        return self._stale

    async def start(self) -> None:
        self._task = asyncio.create_task(self._beat())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    @property
    def cancel_requested(self) -> bool:
        """A newer message asked to restart this turn (cooperative: only a safe boundary may
        act on it, and never inside PREPARED -> EXECUTING -> external call -> finalize)."""
        return self._lease.cancel_requested

    def acknowledge_cancel(self) -> None:
        self._lease = self._lease.model_copy(update={"cancel_requested": False})

    def ensure_active(self) -> None:
        """Called at safe boundaries (before each new step)."""
        if self._stale or self._clock.now() >= self._lease.expires_at:
            self._stale = True
            raise StaleWorkerError(f"lease lost for {self._lease.key.conversation_id}")

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                renewed = await self._store.heartbeat(self._lease, self._ttl)
            except Exception:
                continue  # transient store error: the local expiry check still protects us
            if renewed is None:
                self._stale = True  # lost for good: someone else owns the conversation
                return
            self._lease = renewed  # also carries the latest cancel_requested
