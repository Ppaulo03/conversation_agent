from __future__ import annotations

from datetime import timedelta
from typing import Protocol

from conversation_agent.core.models.runtime import ConversationKey, Lease


class ConversationLeaseStore(Protocol):
    """Conversation lease + fencing token (DESIGN §20.1).

    Every successful `acquire` increments `conversation_epoch`. Time comes from the injected
    `Clock`, never from the database.
    """

    async def acquire(self, key: ConversationKey, owner: str, ttl: timedelta) -> Lease | None:
        """None when another owner holds an unexpired lease."""
        ...

    async def heartbeat(self, lease: Lease, ttl: timedelta) -> Lease | None:
        """Compare-and-set on (owner, epoch). None means the lease was lost."""
        ...

    async def release(self, lease: Lease) -> None: ...
