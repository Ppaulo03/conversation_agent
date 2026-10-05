from __future__ import annotations

from datetime import datetime, timedelta

from conversation_agent.adapters.postgres.coordination import CoordinationTime
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.rows import OUTBOX_COLUMNS, outbox_from_row
from conversation_agent.core.models.runtime import OutboundMessage, OutboxStatus, SendResult
from conversation_agent.ports.clock import Clock
from conversation_agent.ports.coordination import CoordinationClock

DEFAULT_UNKNOWN_BLOCKS_FOR = timedelta(hours=24)  # RelayPlane's default idempotency window
_O_COLUMNS = ", ".join(f"o.{c.strip()}" for c in OUTBOX_COLUMNS.split(","))


class PostgresOutboxStore:
    def __init__(
        self,
        db: PostgresDatabase,
        clock: Clock,
        *,
        max_attempts: int = 5,
        coordination: CoordinationClock | None = None,
    ) -> None:
        self._time = coordination or CoordinationTime(db)
        self._db = db
        self._clock = clock
        self._max_attempts = max_attempts

    async def claim_ready(
        self,
        owner: str,
        limit: int,
        claim_ttl: timedelta,
        scope: str | None = None,
        unknown_blocks_for: timedelta = DEFAULT_UNKNOWN_BLOCKS_FOR,
    ) -> list[OutboundMessage]:
        """PENDING-and-due or stale-SENDING rows -> SENDING. A stale SENDING row is a message
        whose sender died mid-flight: it is retried with the *same* payload and idempotency
        key (never regenerated).

        A message is only claimable when no EARLIER message of its conversation is still waiting to
        go out (PENDING, SENDING, RECONCILING) or has an unknown outcome inside the idempotency
        window (`unknown_blocks_for` since its first send): otherwise a second worker could send
        part 2 of a reply before part 1. Past that window an unresolved UNKNOWN stops blocking, so
        one stuck message cannot silence the conversation for ever."""
        now = await self._time.now()
        rows = await self._db.pool.fetch(
            f"""
            WITH picked AS (
                SELECT o.tenant_id, o.outbox_id FROM outbox_messages o
                 WHERE ((o.status = 'PENDING' AND o.available_at <= $1)
                    OR (o.status = 'SENDING' AND o.claim_expires_at <= $1))
                   AND ($5::text IS NULL OR EXISTS (
                          SELECT 1 FROM conversation_states c
                           WHERE c.tenant_id = o.tenant_id
                             AND c.conversation_id = o.conversation_id
                             AND c.scope = $5))
                   AND NOT EXISTS (
                          SELECT 1 FROM outbox_messages p
                           WHERE p.tenant_id = o.tenant_id
                             AND p.conversation_id = o.conversation_id
                             AND p.conversation_seq < o.conversation_seq
                             AND (p.status IN ('PENDING', 'SENDING', 'RECONCILING')
                                  OR (p.status = 'UNKNOWN'
                                      AND COALESCE(p.first_sent_at, p.created_at) > $6)))
                 ORDER BY o.created_at, o.conversation_seq
                 LIMIT $2 FOR UPDATE SKIP LOCKED
            )
            UPDATE outbox_messages o
               SET status = 'SENDING', claim_owner = $3, claim_expires_at = $4,
                   attempts = o.attempts + 1, updated_at = $1,
                   first_sent_at = COALESCE(o.first_sent_at, $1)
              FROM picked
             WHERE o.tenant_id = picked.tenant_id AND o.outbox_id = picked.outbox_id
            RETURNING {_O_COLUMNS}
            """,
            now,
            limit,
            owner,
            now + claim_ttl,
            scope,
            now - unknown_blocks_for,
        )
        return [outbox_from_row(r) for r in rows]

    async def record_result(
        self, message: OutboundMessage, owner: str, result: SendResult, retry_after: timedelta
    ) -> None:
        """Compare-and-set on (status=SENDING, claim_owner): a sender that lost its claim to
        another worker cannot overwrite the newer outcome."""
        now = await self._time.now()
        status = result.status
        available_at = now
        if (
            status is OutboxStatus.FAILED
            and result.retryable
            and message.attempts < self._max_attempts
        ):
            status, available_at = OutboxStatus.PENDING, now + retry_after
        # The channel's own acceptance time, or NULL. Never the local/db clock: comparing a
        # provider-domain reply time with a local one would fake (in)eligibility (INV-026).
        accepted_at = result.provider_accepted_at if status is OutboxStatus.ACCEPTED else None
        await self._db.pool.execute(
            """
            UPDATE outbox_messages
               SET status = $4, provider_message_id = COALESCE($5, provider_message_id),
                   provider_accepted_at = COALESCE($6, provider_accepted_at),
                   channel_message_id = COALESCE($9, channel_message_id),
                   available_at = $7, claim_owner = NULL, claim_expires_at = NULL, updated_at = $8
             WHERE tenant_id = $1 AND outbox_id = $2 AND status = 'SENDING' AND claim_owner = $3
            """,
            message.tenant_id,
            message.outbox_id,
            owner,
            status.value,
            result.provider_message_id,
            accepted_at,
            available_at,
            now,
            result.channel_message_id,
        )

    async def claim_unsettled(
        self,
        owner: str,
        limit: int,
        claim_ttl: timedelta,
        poll_after: timedelta,
        scope: str | None = None,
    ) -> list[OutboundMessage]:
        now = await self._time.now()
        rows = await self._db.pool.fetch(
            f"""
            WITH picked AS (
                SELECT o.tenant_id, o.outbox_id FROM outbox_messages o
                 WHERE ((o.status = 'UNKNOWN' AND o.available_at <= $1)
                    OR (o.status = 'QUEUED' AND o.updated_at <= $5)
                    OR (o.status = 'RECONCILING' AND o.claim_expires_at <= $1))
                   AND ($6::text IS NULL OR EXISTS (
                          SELECT 1 FROM conversation_states c
                           WHERE c.tenant_id = o.tenant_id
                             AND c.conversation_id = o.conversation_id
                             AND c.scope = $6))
                 ORDER BY o.updated_at
                 LIMIT $2 FOR UPDATE SKIP LOCKED
            )
            UPDATE outbox_messages o
               SET status = 'RECONCILING', claim_owner = $3, claim_expires_at = $4,
                   updated_at = $1
              FROM picked
             WHERE o.tenant_id = picked.tenant_id AND o.outbox_id = picked.outbox_id
            RETURNING {_O_COLUMNS}
            """,
            now,
            limit,
            owner,
            now + claim_ttl,
            now - poll_after,
            scope,
        )
        return [outbox_from_row(r) for r in rows]

    async def record_reconciliation(
        self,
        message: OutboundMessage,
        owner: str,
        *,
        found: SendResult | None,
        resend: bool,
        retry_after: timedelta,
        note: str | None = None,
        resend_until: datetime | None = None,
    ) -> None:
        now = await self._time.now()
        if found is not None:
            status = found.status
            # UNKNOWN/QUEUED are asked again later, not in a tight loop
            waiting = status in (OutboxStatus.UNKNOWN, OutboxStatus.QUEUED)
            available_at = now + retry_after if waiting else now
            accepted_at = found.provider_accepted_at if status is OutboxStatus.ACCEPTED else None
            provider_id = found.provider_message_id
        elif resend:
            status, available_at, accepted_at, provider_id = OutboxStatus.PENDING, now, None, None
        else:
            status, available_at, accepted_at, provider_id = (
                OutboxStatus.UNKNOWN,
                now + retry_after,
                None,
                None,
            )
        await self._db.pool.execute(
            """
            UPDATE outbox_messages
               SET status = $4, provider_message_id = COALESCE($5, provider_message_id),
                   provider_accepted_at = COALESCE($6, provider_accepted_at),
                   channel_message_id = COALESCE($10, channel_message_id),
                   available_at = $7, claim_owner = NULL, claim_expires_at = NULL,
                   reconcile_attempts = reconcile_attempts + 1,
                   last_error = COALESCE($8, last_error), updated_at = $9,
                   resend_authorized_until = COALESCE($11, resend_authorized_until)
             WHERE tenant_id = $1 AND outbox_id = $2 AND status = 'RECONCILING'
               AND claim_owner = $3
            """,
            message.tenant_id,
            message.outbox_id,
            owner,
            status.value,
            provider_id,
            accepted_at,
            available_at,
            note,
            now,
            found.channel_message_id if found is not None else None,
            resend_until if resend else None,
        )

    async def apply_channel_status(
        self, tenant_id: str, channel_message_id: str, result: SendResult
    ) -> bool:
        """Forward-only: the row may be waiting (SENDING/QUEUED/UNKNOWN/RECONCILING/PENDING); a
        final ACCEPTED or FAILED is never rewritten and a late QUEUED never regresses anything."""
        now = await self._time.now()
        accepted_at = (
            result.provider_accepted_at if result.status is OutboxStatus.ACCEPTED else None
        )
        updated = await self._db.pool.execute(
            """
            UPDATE outbox_messages
               SET status = $3, provider_message_id = COALESCE($4, provider_message_id),
                   provider_accepted_at = COALESCE($5, provider_accepted_at),
                   claim_owner = NULL, claim_expires_at = NULL, updated_at = $6
             WHERE tenant_id = $1 AND channel_message_id = $2
               AND status IN ('SENDING', 'QUEUED', 'UNKNOWN', 'RECONCILING', 'PENDING')
               AND NOT ($3 = 'QUEUED' AND status IN ('QUEUED', 'UNKNOWN'))
            """,
            tenant_id,
            channel_message_id,
            result.status.value,
            result.provider_message_id,
            accepted_at,
            now,
        )
        return not updated.endswith(" 0")

    async def get(self, tenant_id: str, outbox_id: str) -> OutboundMessage | None:
        r = await self._db.pool.fetchrow(
            f"SELECT {OUTBOX_COLUMNS} FROM outbox_messages WHERE tenant_id=$1 AND outbox_id=$2",
            tenant_id,
            outbox_id,
        )
        return outbox_from_row(r) if r else None
