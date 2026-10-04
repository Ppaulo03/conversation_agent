from __future__ import annotations

from conversation_agent.adapters.postgres.conversations import ensure_conversation
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.models.runtime import ConversationKey, InboundEvent
from conversation_agent.ports.clock import Clock


class PostgresInboxStore:
    def __init__(
        self, db: PostgresDatabase, clock: Clock, *, restart_on_new_message: bool = False
    ) -> None:
        self._db = db
        self._clock = clock
        # Policy for a message that arrives while a turn is processing: `queue` (default) lets
        # the turn finish; `restart` flags it so the worker stops at the next safe boundary.
        self._restart = restart_on_new_message

    async def insert_if_absent(self, event: InboundEvent) -> bool:
        """One transaction: the conversation row (first contact) + the event. Dedupe is the
        UNIQUE(tenant, channel, event_id) constraint, so concurrent redeliveries race safely
        (INV-020)."""
        async with self._db.pool.acquire() as conn, conn.transaction():
            await ensure_conversation(conn, event.identity, self._clock.now())
            row = await conn.fetchrow(
                """
                INSERT INTO inbox_events (tenant_id, channel_id, event_id, conversation_id,
                    contact_id, session_id, source_sequence, occurred_at, received_at, text,
                    provider_occurred_at, reply_to_provider_message_id, media, kind,
                    provider_message_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                ON CONFLICT (tenant_id, channel_id, event_id) DO NOTHING
                RETURNING id
                """,
                event.tenant_id,
                event.channel_id,
                event.event_id,
                event.conversation_id,
                event.contact_id,
                event.session_id,
                event.source_sequence,
                event.occurred_at,
                event.received_at,
                event.text,
                event.provider_occurred_at,
                event.reply_to_provider_message_id,
                [m.model_dump(mode="json") for m in event.media],
                event.kind,
                event.provider_message_id,
            )
            if row is not None and self._restart:
                await conn.execute(
                    "UPDATE conversation_states SET cancel_requested = true "
                    "WHERE tenant_id=$1 AND conversation_id=$2 AND lease_owner IS NOT NULL",
                    event.tenant_id,
                    event.conversation_id,
                )
        return row is not None

    async def withdraw_unprocessed(
        self, tenant_id: str, channel_id: str, provider_message_id: str
    ) -> int:
        status = await self._db.pool.execute(
            "UPDATE inbox_events SET status='DEAD' WHERE tenant_id=$1 AND channel_id=$2 "
            "AND provider_message_id=$3 AND status='READY'",
            tenant_id,
            channel_id,
            provider_message_id,
        )
        return int(status.rsplit(" ", 1)[-1])

    async def list_ready_conversations(self, limit: int = 50) -> list[ConversationKey]:
        rows = await self._db.pool.fetch(
            """
            SELECT tenant_id, conversation_id, min(id) AS first_id FROM inbox_events
             WHERE status IN ('READY', 'CLAIMED')
             GROUP BY tenant_id, conversation_id
             ORDER BY first_id LIMIT $1
            """,
            limit,
        )
        return [
            ConversationKey(tenant_id=r["tenant_id"], conversation_id=r["conversation_id"])
            for r in rows
        ]
