"""Conversation lease + fencing token over PostgreSQL (DESIGN §20.1).

The conversation row is the single serialisation point: acquiring a lease and every fenced
transaction touch that row, so a takeover and a zombie's commit can never interleave.
"""

from __future__ import annotations

from datetime import timedelta

from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.models.runtime import ConversationKey, Lease
from conversation_agent.ports.clock import Clock


class PostgresLeaseStore:
    def __init__(self, db: PostgresDatabase, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    async def acquire(self, key: ConversationKey, owner: str, ttl: timedelta) -> Lease | None:
        now = self._clock.now()
        expires = now + ttl
        row = await self._db.pool.fetchrow(
            """
            UPDATE conversation_states
               SET lease_owner = $3, lease_expires_at = $4,
                   conversation_epoch = conversation_epoch + 1, updated_at = $5
             WHERE tenant_id = $1 AND conversation_id = $2
               AND (lease_owner IS NULL OR lease_expires_at <= $5)
            RETURNING conversation_epoch
            """,
            key.tenant_id,
            key.conversation_id,
            owner,
            expires,
            now,
        )
        if row is None:
            return None
        return Lease(key=key, owner=owner, epoch=row["conversation_epoch"], expires_at=expires)

    async def heartbeat(self, lease: Lease, ttl: timedelta) -> Lease | None:
        now = self._clock.now()
        expires = now + ttl
        row = await self._db.pool.fetchrow(
            """
            UPDATE conversation_states
               SET lease_expires_at = $5, updated_at = $6
             WHERE tenant_id = $1 AND conversation_id = $2
               AND lease_owner = $3 AND conversation_epoch = $4
            RETURNING 1
            """,
            lease.key.tenant_id,
            lease.key.conversation_id,
            lease.owner,
            lease.epoch,
            expires,
            now,
        )
        return lease.model_copy(update={"expires_at": expires}) if row else None

    async def release(self, lease: Lease) -> None:
        await self._db.pool.execute(
            """
            UPDATE conversation_states
               SET lease_owner = NULL, lease_expires_at = NULL, updated_at = $5
             WHERE tenant_id = $1 AND conversation_id = $2
               AND lease_owner = $3 AND conversation_epoch = $4
            """,
            lease.key.tenant_id,
            lease.key.conversation_id,
            lease.owner,
            lease.epoch,
            self._clock.now(),
        )
