from __future__ import annotations

from datetime import timedelta

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

    async def waiting(self, scope: str | None = None) -> int:
        """How many events are waiting for a turn to open (a debounce window may hold them)."""
        value = await self._db.pool.fetchval(
            """
            SELECT count(*) FROM inbox_events e
              JOIN conversation_states c
                ON c.tenant_id = e.tenant_id AND c.conversation_id = e.conversation_id
             WHERE e.status = 'READY' AND ($1::text IS NULL OR c.scope = $1)
            """,
            scope,
        )
        return int(value or 0)

    async def insert_if_absent(self, event: InboundEvent) -> bool:
        """One transaction: the conversation row (first contact) + the event. Dedupe is the
        UNIQUE(tenant, channel, event_id) constraint, so concurrent redeliveries race safely
        (INV-020)."""
        async with self._db.pool.acquire() as conn, conn.transaction():
            await ensure_conversation(conn, event.identity, self._clock.now(), event.scope)
            row = await conn.fetchrow(
                """
                INSERT INTO inbox_events (tenant_id, channel_id, event_id, conversation_id,
                    contact_id, session_id, source_sequence, occurred_at, received_at, text,
                    provider_occurred_at, reply_to_provider_message_id, media, kind,
                    provider_message_id, trace_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
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
                event.trace_id,
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

    async def list_ready_conversations(
        self,
        limit: int = 50,
        scope: str | None = None,
        quiet: timedelta | None = None,
        max_wait: timedelta | None = None,
    ) -> list[ConversationKey]:
        """Conversations with work to do. With `quiet`, a conversation whose newest message is
        younger than that waits (the contact may still be typing), but never longer than
        `max_wait` since its OLDEST waiting message; a turn already in progress is never held
        back. Measured on the database clock."""
        rows = await self._db.pool.fetch(
            """
            SELECT e.tenant_id, e.conversation_id, min(e.id) AS first_id FROM inbox_events e
              JOIN conversation_states c
                ON c.tenant_id = e.tenant_id AND c.conversation_id = e.conversation_id
             WHERE e.status IN ('READY', 'CLAIMED') AND ($2::text IS NULL OR c.scope = $2)
               AND NOT EXISTS (
                   SELECT 1 FROM turns t
                    WHERE t.tenant_id=e.tenant_id AND t.conversation_id=e.conversation_id
                      AND t.status='PROCESSING' AND t.deferred_until > clock_timestamp()
               )
             GROUP BY e.tenant_id, e.conversation_id
            HAVING $3::float8 IS NULL
                OR bool_or(e.status = 'CLAIMED')
                OR max(e.inserted_at) <= clock_timestamp() - make_interval(secs => $3)
                OR min(e.inserted_at) <= clock_timestamp() - make_interval(secs => $4)
             ORDER BY first_id LIMIT $1
            """,
            limit,
            scope,
            quiet.total_seconds() if quiet else None,
            (max_wait or quiet or timedelta(0)).total_seconds(),
        )
        return [
            ConversationKey(tenant_id=r["tenant_id"], conversation_id=r["conversation_id"])
            for r in rows
        ]
