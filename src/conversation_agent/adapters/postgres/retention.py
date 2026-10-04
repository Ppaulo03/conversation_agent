"""Retention and erasure over PostgreSQL (LGPD, DESIGN §38). See `core.retention` for the model.

Every operation is ONE transaction that also writes its audit entry (counts and a non-reversible
person reference, never content). Time comes from the database clock (INV-032) unless the caller
passes `now`. An erasure refuses to run while something about the contact is still in motion (an
open turn, an unfinished external effect, a message still to be delivered, a pending
confirmation); the blockers are reported so an operator knows what to wait for, and `force`
cancels only what can be cancelled safely (undelivered messages, pending confirmations).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from conversation_agent.adapters.postgres.audit import insert_audit
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.models.audit import AuditEntry, subject_ref
from conversation_agent.core.retention import (
    ContactFootprint,
    ErasureResult,
    RetentionPolicy,
    RetentionResult,
)

_REDACTED_JOURNAL = {"redacted": True}
_OUTBOX_UNDELIVERED = ("PENDING", "SENDING", "QUEUED", "UNKNOWN", "RECONCILING")
_INVOCATION_DONE = ("SUCCEEDED", "FAILED", "RECONCILED")  # HUMAN_HANDOFF: a person still owns it


class _Rollback(Exception):
    """Aborts a dry-run transaction after its counts were collected."""


class RetentionService:
    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db

    # ------------------------------------------------------------------ policy

    async def policy_for(self, tenant_id: str) -> RetentionPolicy:
        raw = await self._db.pool.fetchval(
            "SELECT policy FROM tenant_retention WHERE tenant_id = $1", tenant_id
        )
        return RetentionPolicy.model_validate(raw) if raw else RetentionPolicy()

    async def set_policy(self, tenant_id: str, policy: RetentionPolicy, *, actor: str) -> None:
        async with self._db.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO tenant_retention (tenant_id, policy, updated_by) VALUES ($1,$2,$3) "
                "ON CONFLICT (tenant_id) DO UPDATE SET policy = $2, updated_by = $3, "
                "updated_at = clock_timestamp()",
                tenant_id,
                policy.model_dump(mode="json"),
                actor,
            )
            await insert_audit(
                conn,
                AuditEntry(
                    tenant_id=tenant_id,
                    actor=actor,
                    action="retention.policy",
                    subject_type="tenant",
                    subject_id=tenant_id,
                    details=policy.model_dump(mode="json"),
                ),
            )

    # ------------------------------------------------------------------ locate

    async def locate_contact(self, tenant_id: str, contact_id: str) -> ContactFootprint:
        """Counts per table of what the runtime holds about this contact."""
        async with self._db.pool.acquire() as conn:
            conversations = await self._conversations(conn, tenant_id, contact_id)
            return ContactFootprint(
                await self._footprint(conn, tenant_id, contact_id, conversations)
            )

    # ------------------------------------------------------------------ erase a contact

    async def erase_contact(
        self,
        tenant_id: str,
        contact_id: str,
        *,
        actor: str,
        reason: str,
        force: bool = False,
    ) -> ErasureResult:
        reference = subject_ref(tenant_id, contact_id)
        async with self._db.pool.acquire() as conn:
            result = await self._erase(conn, tenant_id, contact_id, reference, actor, reason, force)
        return result

    async def _erase(
        self,
        conn: Any,
        tenant_id: str,
        contact_id: str,
        ref: str,
        actor: str,
        reason: str,
        force: bool,
    ) -> ErasureResult:
        async with conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"erase/{tenant_id}/{ref}"
            )
            conversations = await self._conversations(conn, tenant_id, contact_id)
            found = await self._footprint(conn, tenant_id, contact_id, conversations)
            if not any(found.values()):
                return ErasureResult("nothing_found", reference=ref)
            blockers = await self._blockers(conn, tenant_id, conversations, force)
            if blockers:
                await self._audit(
                    conn, tenant_id, actor, ref, "refused", reason, {"blockers": blockers}
                )
                return ErasureResult("blocked", blockers=tuple(blockers), reference=ref)
            counts = await self._do_erase(conn, tenant_id, contact_id, ref, conversations)
            await self._audit(
                conn,
                tenant_id,
                actor,
                ref,
                "ok",
                reason,
                {**counts, "conversations": len(conversations), "forced": force},
            )
            return ErasureResult("erased", counts=counts, reference=ref)

    @staticmethod
    async def _conversations(conn: Any, tenant_id: str, contact_id: str) -> list[str]:
        rows = await conn.fetch(
            "SELECT conversation_id FROM conversation_states WHERE tenant_id=$1 AND contact_id=$2 "
            "UNION SELECT conversation_id FROM inbox_events WHERE tenant_id=$1 AND contact_id=$2 "
            "UNION SELECT conversation_id FROM outbox_messages "
            "WHERE tenant_id=$1 AND contact_id=$2",
            tenant_id,
            contact_id,
        )
        return [r["conversation_id"] for r in rows]

    @staticmethod
    async def _footprint(
        conn: Any, tenant_id: str, contact_id: str, conversations: list[str]
    ) -> dict[str, int]:
        async def count(sql: str, *args: Any) -> int:
            return int(await conn.fetchval(sql, *args))

        by_conv = "tenant_id = $1 AND conversation_id = ANY($2)"
        return {
            "conversation_states": await count(
                f"SELECT count(*) FROM conversation_states WHERE {by_conv}",
                tenant_id,
                conversations,
            ),
            "turns": await count(
                f"SELECT count(*) FROM turns WHERE {by_conv}", tenant_id, conversations
            ),
            "turn_journal": await count(
                f"SELECT count(*) FROM turn_journal WHERE {by_conv}", tenant_id, conversations
            ),
            "inbox_events": await count(
                "SELECT count(*) FROM inbox_events WHERE tenant_id = $1 AND (contact_id = $3 "
                "OR conversation_id = ANY($2))",
                tenant_id,
                conversations,
                contact_id,
            ),
            "outbox_messages": await count(
                "SELECT count(*) FROM outbox_messages WHERE tenant_id = $1 AND (contact_id = $3 "
                "OR conversation_id = ANY($2))",
                tenant_id,
                conversations,
                contact_id,
            ),
            "tool_invocations": await count(
                f"SELECT count(*) FROM tool_invocations WHERE {by_conv}", tenant_id, conversations
            ),
            "pending_actions": await count(
                f"SELECT count(*) FROM pending_actions WHERE {by_conv}", tenant_id, conversations
            ),
            "scheduled_events": await count(
                "SELECT count(*) FROM scheduled_events WHERE tenant_id = $1 AND "
                "(payload->>'contact_id' = $2 OR payload->>'conversation_id' = ANY($3))",
                tenant_id,
                contact_id,
                conversations,
            ),
        }

    @staticmethod
    async def _blockers(
        conn: Any, tenant_id: str, conversations: list[str], force: bool
    ) -> list[str]:
        """What still moves. Open turns, live leases and unfinished external effects are never
        forced; undelivered messages and pending confirmations are, by cancelling them."""
        blockers: list[str] = []
        by_conv = "tenant_id = $1 AND conversation_id = ANY($2)"
        if await conn.fetchval(
            f"SELECT count(*) FROM turns WHERE {by_conv} AND status = 'PROCESSING'",
            tenant_id,
            conversations,
        ):
            blockers.append("turn_in_progress")
        if await conn.fetchval(
            f"SELECT count(*) FROM conversation_states WHERE {by_conv} "
            "AND lease_expires_at > clock_timestamp()",
            tenant_id,
            conversations,
        ):
            blockers.append("conversation_leased")
        if await conn.fetchval(
            f"SELECT count(*) FROM tool_invocations WHERE {by_conv} AND status <> ALL($3)",
            tenant_id,
            conversations,
            list(_INVOCATION_DONE),
        ):
            blockers.append("external_effect_unfinished")
        if not force:
            if await conn.fetchval(
                f"SELECT count(*) FROM outbox_messages WHERE {by_conv} AND status = ANY($3)",
                tenant_id,
                conversations,
                list(_OUTBOX_UNDELIVERED),
            ):
                blockers.append("message_awaiting_delivery")
            if await conn.fetchval(
                f"SELECT count(*) FROM pending_actions WHERE {by_conv} "
                "AND status = 'PENDING_CONFIRMATION'",
                tenant_id,
                conversations,
            ):
                blockers.append("confirmation_pending")
        return blockers

    @staticmethod
    async def _do_erase(
        conn: Any, tenant_id: str, contact_id: str, ref: str, conversations: list[str]
    ) -> dict[str, int]:
        counts: dict[str, int] = {}

        async def run(name: str, sql: str, *args: Any) -> None:
            status = await conn.execute(sql, *args)
            counts[name] = int(status.split()[-1])

        # Cancel what can still move (only reachable with `force`; otherwise it blocked).
        await conn.execute(
            "UPDATE pending_actions SET status = 'INVALIDATED' WHERE tenant_id = $1 "
            "AND conversation_id = ANY($2) AND status = 'PENDING_CONFIRMATION'",
            tenant_id,
            conversations,
        )
        await run(
            "action_confirmations",
            "DELETE FROM action_confirmations WHERE tenant_id = $1 AND action_id IN "
            "(SELECT action_id FROM pending_actions WHERE tenant_id = $1 "
            "AND conversation_id = ANY($2))",
            tenant_id,
            conversations,
        )
        await run(
            "pending_actions",
            "DELETE FROM pending_actions WHERE tenant_id = $1 AND conversation_id = ANY($2)",
            tenant_id,
            conversations,
        )
        await run(
            "turn_journal",
            "DELETE FROM turn_journal WHERE tenant_id = $1 AND conversation_id = ANY($2)",
            tenant_id,
            conversations,
        )
        await run(
            "turns",
            "DELETE FROM turns WHERE tenant_id = $1 AND conversation_id = ANY($2)",
            tenant_id,
            conversations,
        )
        await run(
            "scheduled_events",
            "DELETE FROM scheduled_events WHERE tenant_id = $1 AND "
            "(payload->>'contact_id' = $2 OR payload->>'conversation_id' = ANY($3))",
            tenant_id,
            contact_id,
            conversations,
        )
        # Tombstones: the dedupe record of an inbound event and the idempotency record of a send
        # stay (a redelivery must not become a new turn, a send must not repeat) but carry no
        # content and no identifier of the person.
        await run(
            "inbox_events",
            "UPDATE inbox_events SET text = '', media = '[]'::jsonb, contact_id = $4, "
            "conversation_id = $4, session_id = $4, reply_to_provider_message_id = NULL, "
            "provider_message_id = NULL, "
            "status = CASE WHEN status IN ('CONSUMED', 'DEAD') THEN status ELSE 'DEAD' END "
            "WHERE tenant_id = $1 AND (contact_id = $2 OR conversation_id = ANY($3))",
            tenant_id,
            contact_id,
            conversations,
            ref,
        )
        await run(
            "outbox_messages",
            "UPDATE outbox_messages SET text = '', contact_id = $4, conversation_id = $4, "
            "last_error = NULL, "
            "status = CASE WHEN status = ANY($5) THEN 'SUPERSEDED' ELSE status END "
            "WHERE tenant_id = $1 AND (contact_id = $2 OR conversation_id = ANY($3))",
            tenant_id,
            contact_id,
            conversations,
            ref,
            list(_OUTBOX_UNDELIVERED),
        )
        await run(
            "tool_invocations",
            "UPDATE tool_invocations SET conversation_id = $3, session_id = $3, "
            "request_json = '{}'::jsonb, context_json = '{}'::jsonb, intent_json = '{}'::jsonb, "
            "result_json = NULL, error_json = NULL, provider_metadata = '{}'::jsonb, "
            "args_hash = 'erased' WHERE tenant_id = $1 AND conversation_id = ANY($2)",
            tenant_id,
            conversations,
            ref,
        )
        await run(
            "conversation_states",
            "DELETE FROM conversation_states WHERE tenant_id = $1 AND conversation_id = ANY($2)",
            tenant_id,
            conversations,
        )
        return counts

    @staticmethod
    async def _audit(
        conn: Any,
        tenant_id: str,
        actor: str,
        ref: str,
        outcome: str,
        reason: str,
        details: dict[str, Any],
    ) -> None:
        await insert_audit(
            conn,
            AuditEntry(
                tenant_id=tenant_id,
                actor=actor,
                action="contact.erase",
                subject_type="contact",
                subject_id=ref,
                outcome="ok" if outcome == "ok" else "refused",
                details={"reason": reason, **details},
            ),
        )

    # ------------------------------------------------------------------ retention windows

    async def apply_retention(
        self,
        tenant_id: str,
        *,
        actor: str,
        now: datetime | None = None,
        policy: RetentionPolicy | None = None,
        dry_run: bool = False,
    ) -> RetentionResult:
        """Enforces the tenant's windows. With `dry_run` the counts are real but nothing changes."""
        policy = policy or await self.policy_for(tenant_id)
        counts: dict[str, int] = {}
        try:
            async with self._db.pool.acquire() as conn, conn.transaction():
                clock = now or await conn.fetchval("SELECT clock_timestamp()")
                await self._enforce(conn, tenant_id, policy, clock, counts)
                if dry_run:
                    raise _Rollback  # counts are real; nothing is kept
                await insert_audit(
                    conn,
                    AuditEntry(
                        tenant_id=tenant_id,
                        actor=actor,
                        action="retention.apply",
                        subject_type="tenant",
                        subject_id=tenant_id,
                        details=dict(counts),
                    ),
                )
        except _Rollback:
            pass
        return RetentionResult(dry_run, counts)

    @staticmethod
    async def _enforce(
        conn: Any, tenant_id: str, policy: RetentionPolicy, now: datetime, counts: dict[str, int]
    ) -> None:
        async def run(name: str, sql: str, *args: Any) -> None:
            counts[name] = int((await conn.execute(sql, *args)).split()[-1])

        journal_cut = now - timedelta(days=policy.journal_payload_days)
        message_cut = now - timedelta(days=policy.message_days)
        state_cut = now - timedelta(days=policy.conversation_state_days)
        media_cut = now - timedelta(days=policy.media_reference_days)

        await run(
            "journal_payloads",
            "UPDATE turn_journal j SET payload = $3::jsonb FROM turns t "
            "WHERE j.tenant_id = $1 AND t.tenant_id = j.tenant_id AND t.turn_id = j.turn_id "
            "AND t.status IN ('COMPLETED', 'FAILED', 'CANCELLED') "
            "AND COALESCE(t.completed_at, t.created_at) < $2 AND j.payload <> $3::jsonb",
            tenant_id,
            journal_cut,
            _REDACTED_JOURNAL,
        )
        await run(
            "inbound_messages",
            "UPDATE inbox_events SET text = '' WHERE tenant_id = $1 AND received_at < $2 "
            "AND status IN ('CONSUMED', 'DEAD') AND text <> ''",
            tenant_id,
            message_cut,
        )
        await run(
            "outbound_messages",
            "UPDATE outbox_messages SET text = '' WHERE tenant_id = $1 AND created_at < $2 "
            "AND status IN ('ACCEPTED', 'FAILED', 'SUPERSEDED') AND text <> ''",
            tenant_id,
            message_cut,
        )
        await run(
            "turn_texts",
            "UPDATE turns SET user_text = '' WHERE tenant_id = $1 AND created_at < $2 "
            "AND status IN ('COMPLETED', 'FAILED', 'CANCELLED') AND user_text <> ''",
            tenant_id,
            message_cut,
        )
        await run(
            "media_references",
            "UPDATE inbox_events SET media = '[]'::jsonb WHERE tenant_id = $1 AND received_at < $2 "
            "AND media <> '[]'::jsonb",
            tenant_id,
            media_cut,
        )
        await run(
            "conversation_states",
            "UPDATE conversation_states c SET state_json = '{}'::jsonb "
            "WHERE c.tenant_id = $1 AND c.updated_at < $2 AND c.ownership = 'BOT' "
            "AND c.state_json <> '{}'::jsonb "
            "AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.tenant_id = c.tenant_id "
            "AND t.conversation_id = c.conversation_id AND t.status = 'PROCESSING') "
            "AND NOT EXISTS (SELECT 1 FROM pending_actions p WHERE p.tenant_id = c.tenant_id "
            "AND p.conversation_id = c.conversation_id AND p.status = 'PENDING_CONFIRMATION')",
            tenant_id,
            state_cut,
        )
