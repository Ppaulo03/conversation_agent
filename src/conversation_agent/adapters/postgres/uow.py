"""ConversationUnitOfWork over PostgreSQL.

Fencing: every UoW starts by taking a `FOR SHARE` lock on the conversation row *conditioned
on* (owner, epoch). A takeover (`UPDATE ... conversation_epoch + 1`) needs an exclusive row
lock, so it waits for in-flight UoWs and any UoW that starts afterwards fails the check.
A zombie therefore can never commit conversational state after a takeover (INV-009).

No UoW is ever held across LLM or external I/O: each one is a short local transaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import asyncpg

from conversation_agent.adapters.postgres.coordination import CoordinationTime
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.rows import (
    INVOCATION_COLUMNS,
    invocation_from_row,
)
from conversation_agent.core.canonical import stable_hash
from conversation_agent.core.errors import FencingError, JournalConflictError
from conversation_agent.core.models.actions import (
    ActionConfirmation,
    PendingAction,
    PendingActionStatus,
    PromptRecord,
)
from conversation_agent.core.models.conversation import ConversationIdentity, ConversationState
from conversation_agent.core.models.journal import JournalEntry, JournalStepType
from conversation_agent.core.models.runtime import (
    FenceToken,
    InboundRef,
    OpenedTurn,
    OutboundMessage,
    OutboxStatus,
    Ownership,
    ToolInvocation,
)
from conversation_agent.core.models.tooling import CapabilityRequest
from conversation_agent.ports.clock import Clock
from conversation_agent.ports.uow import StoredConversation

_JOURNAL_COLUMNS = "turn_id, step_index, step_type, request_hash, logical_step_id, payload"


def _journal_from_row(r: asyncpg.Record) -> JournalEntry:
    return JournalEntry(
        turn_id=r["turn_id"],
        step_index=r["step_index"],
        step_type=JournalStepType(r["step_type"]),
        request_hash=r["request_hash"],
        logical_step_id=r["logical_step_id"],
        payload=r["payload"],
    )


class _Repo:
    def __init__(self, conn: asyncpg.Connection, fence: FenceToken, clock: Clock) -> None:
        self._c = conn
        self._f = fence
        self._clock = clock


class _StateRepo(_Repo):
    async def load(self) -> StoredConversation:
        r = await self._c.fetchrow(
            "SELECT channel_id, contact_id, session_id, state_json, ownership, version, "
            "last_event_at FROM conversation_states WHERE tenant_id=$1 AND conversation_id=$2",
            self._f.tenant_id,
            self._f.conversation_id,
        )
        assert r is not None  # the fence check already proved the row exists
        return StoredConversation(
            identity=ConversationIdentity(
                tenant_id=self._f.tenant_id,
                channel_id=r["channel_id"],
                conversation_id=self._f.conversation_id,
                session_id=r["session_id"],
                contact_id=r["contact_id"],
            ),
            state=ConversationState.model_validate(r["state_json"] or {}),
            ownership=Ownership(r["ownership"]),
            version=r["version"],
            last_event_at=r["last_event_at"],
        )

    async def save(self, state: ConversationState, *, last_event_at: datetime | None) -> None:
        await self._c.execute(
            """
            UPDATE conversation_states
               SET state_json = $3, version = version + 1, updated_at = $4,
                   last_event_at = GREATEST(last_event_at, $5)  -- GREATEST ignores NULLs
             WHERE tenant_id = $1 AND conversation_id = $2
            """,
            self._f.tenant_id,
            self._f.conversation_id,
            state.model_dump(mode="json"),
            self._clock.now(),
            last_event_at,
        )


def _refs(rows: list[asyncpg.Record]) -> tuple[InboundRef, ...]:
    return tuple(
        InboundRef(
            event_id=r["event_id"],
            provider_occurred_at=r["provider_occurred_at"],
            reply_to_provider_message_id=r["reply_to_provider_message_id"],
        )
        for r in rows
    )


class _TurnRepo(_Repo):
    async def open_next(self, owner: str, now: datetime) -> OpenedTurn | None:
        f = self._f
        identity = (await _StateRepo(self._c, f, self._clock).load()).identity

        open_turn = await self._c.fetchrow(
            "SELECT turn_id, user_text, event_ids, late_event_ids, attempts FROM turns "
            "WHERE tenant_id=$1 AND conversation_id=$2 AND status='PROCESSING'",
            f.tenant_id,
            f.conversation_id,
        )
        if open_turn is not None:
            rows = await self._c.fetch(
                "SELECT event_id, occurred_at, provider_occurred_at, reply_to_provider_message_id "
                "FROM inbox_events WHERE tenant_id=$1 AND conversation_id=$2 AND turn_id=$3 "
                "ORDER BY source_sequence NULLS LAST, occurred_at, received_at, event_id",
                f.tenant_id,
                f.conversation_id,
                open_turn["turn_id"],
            )
            return OpenedTurn(
                last_event_at=max((r["occurred_at"] for r in rows), default=None),
                inbound=_refs(rows),
                turn_id=open_turn["turn_id"],
                identity=identity,
                user_text=open_turn["user_text"],
                event_ids=tuple(open_turn["event_ids"]),
                late_event_ids=tuple(open_turn["late_event_ids"]),
                resumed=True,
                attempts=open_turn["attempts"],
            )

        events = await self._c.fetch(
            """
            SELECT event_id, text, occurred_at, provider_occurred_at,
                   reply_to_provider_message_id FROM inbox_events
             WHERE tenant_id=$1 AND conversation_id=$2 AND status='READY'
             ORDER BY source_sequence NULLS LAST, occurred_at, received_at, event_id
            """,
            f.tenant_id,
            f.conversation_id,
        )
        if not events:
            return None
        event_ids = [e["event_id"] for e in events]
        watermark = await self._c.fetchval(
            "SELECT last_event_at FROM conversation_states "
            "WHERE tenant_id=$1 AND conversation_id=$2",
            f.tenant_id,
            f.conversation_id,
        )
        late = [
            e["event_id"] for e in events if watermark is not None and e["occurred_at"] <= watermark
        ]
        turn_id = stable_hash(f.tenant_id, f.conversation_id, event_ids)[:32]
        text = "\n".join(e["text"] for e in events)
        await self._c.execute(
            "INSERT INTO turns (tenant_id, conversation_id, turn_id, status, event_ids, "
            "late_event_ids, user_text, created_epoch, created_at) "
            "VALUES ($1,$2,$3,'PROCESSING',$4,$5,$6,$7,$8)",
            f.tenant_id,
            f.conversation_id,
            turn_id,
            event_ids,
            late,
            text,
            f.epoch,
            now,
        )
        await self._c.execute(
            "UPDATE inbox_events SET status='CLAIMED', claimed_by=$4, claim_epoch=$5, turn_id=$6 "
            "WHERE tenant_id=$1 AND conversation_id=$2 AND event_id = ANY($3::text[])",
            f.tenant_id,
            f.conversation_id,
            event_ids,
            owner,
            f.epoch,
            turn_id,
        )
        return OpenedTurn(
            last_event_at=max(e["occurred_at"] for e in events),
            inbound=_refs(events),
            turn_id=turn_id,
            identity=identity,
            user_text=text,
            event_ids=tuple(event_ids),
            late_event_ids=tuple(late),
        )

    async def record_failure(self, turn_id: str) -> int:
        """Counts one genuine processing failure and returns the new total."""
        value = await self._c.fetchval(
            "UPDATE turns SET attempts = attempts + 1 "
            "WHERE tenant_id=$1 AND turn_id=$2 AND status='PROCESSING' RETURNING attempts",
            self._f.tenant_id,
            turn_id,
        )
        return int(value or 0)

    async def complete(self, turn_id: str) -> None:
        await self._c.execute(
            "UPDATE turns SET status='COMPLETED', completed_at=$3 "
            "WHERE tenant_id=$1 AND turn_id=$2 AND status='PROCESSING'",
            self._f.tenant_id,
            turn_id,
            self._clock.now(),
        )

    async def fail(self, turn_id: str, reason: str) -> None:
        await self._c.execute(
            "UPDATE turns SET status='FAILED', completed_at=$3, failure_reason=$4 "
            "WHERE tenant_id=$1 AND turn_id=$2 AND status='PROCESSING'",
            self._f.tenant_id,
            turn_id,
            self._clock.now(),
            reason,
        )


class _JournalRepo(_Repo):
    async def get(self, turn_id: str, step_index: int) -> JournalEntry | None:
        r = await self._c.fetchrow(
            f"SELECT {_JOURNAL_COLUMNS} FROM turn_journal "
            "WHERE tenant_id=$1 AND conversation_id=$2 AND turn_id=$3 AND step_index=$4",
            self._f.tenant_id,
            self._f.conversation_id,
            turn_id,
            step_index,
        )
        return _journal_from_row(r) if r else None

    async def find_step(self, turn_id: str, step_type: JournalStepType) -> JournalEntry | None:
        r = await self._c.fetchrow(
            f"SELECT {_JOURNAL_COLUMNS} FROM turn_journal "
            "WHERE tenant_id=$1 AND conversation_id=$2 AND turn_id=$3 AND step_type=$4 "
            "ORDER BY step_index LIMIT 1",
            self._f.tenant_id,
            self._f.conversation_id,
            turn_id,
            step_type.value,
        )
        return _journal_from_row(r) if r else None

    async def append(self, entry: JournalEntry) -> None:
        try:
            await self._c.execute(
                "INSERT INTO turn_journal (tenant_id, conversation_id, turn_id, step_index, "
                "step_type, request_hash, logical_step_id, payload, created_at) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                self._f.tenant_id,
                self._f.conversation_id,
                entry.turn_id,
                entry.step_index,
                entry.step_type.value,
                entry.request_hash,
                entry.logical_step_id,
                entry.payload,
                self._clock.now(),
            )
        except asyncpg.UniqueViolationError as exc:
            raise JournalConflictError(
                f"journal slot already taken: ({entry.turn_id}, {entry.step_index})"
            ) from exc


class _InvocationRepo(_Repo):
    async def create_prepared(self, invocation: ToolInvocation) -> ToolInvocation:
        await self._c.execute(
            """
            INSERT INTO tool_invocations (tenant_id, invocation_id, conversation_id, session_id,
                turn_id, logical_step_id, attempt_semantic_id, action_id, tool_name, capability,
                args_hash, idempotency_key, request_json, context_json, intent_json, status,
                prepared_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,'PREPARED',$16)
            ON CONFLICT (tenant_id, invocation_id) DO NOTHING
            """,
            invocation.tenant_id,
            invocation.invocation_id,
            invocation.conversation_id,
            invocation.session_id,
            invocation.turn_id,
            invocation.logical_step_id,
            invocation.attempt_semantic_id,
            invocation.action_id,
            invocation.tool_name,
            invocation.capability,
            invocation.args_hash,
            invocation.idempotency_key,
            invocation.request.model_dump(mode="json"),
            invocation.context.model_dump(mode="json"),
            invocation.intent.model_dump(mode="json"),
            self._clock.now(),
        )
        stored = await self.get(invocation.invocation_id)
        assert stored is not None
        return stored

    async def get(self, invocation_id: str) -> ToolInvocation | None:
        r = await self._c.fetchrow(
            f"SELECT {INVOCATION_COLUMNS} FROM tool_invocations "
            "WHERE tenant_id=$1 AND invocation_id=$2",
            self._f.tenant_id,
            invocation_id,
        )
        return invocation_from_row(r) if r else None

    async def for_turn(self, turn_id: str) -> list[ToolInvocation]:
        rows = await self._c.fetch(
            f"SELECT {INVOCATION_COLUMNS} FROM tool_invocations "
            "WHERE tenant_id=$1 AND conversation_id=$2 AND turn_id=$3 ORDER BY prepared_at",
            self._f.tenant_id,
            self._f.conversation_id,
            turn_id,
        )
        return [invocation_from_row(r) for r in rows]

    async def mark_applied(self, invocation_id: str, now: datetime) -> None:
        await self._c.execute(
            "UPDATE tool_invocations SET result_application_status='applied', applied_at=$3 "
            "WHERE tenant_id=$1 AND invocation_id=$2 AND result_application_status='pending'",
            self._f.tenant_id,
            invocation_id,
            now,
        )


_ACTION_COLUMNS = (
    "tenant_id, conversation_id, action_id, capability, tool_name, request_json, args_hash, "
    "protected_fields, summary, created_from_turn, latest_prompt_outbox_id, "
    "confirmation_attempts, expires_at, status"
)


def _action_from_row(r: asyncpg.Record) -> PendingAction:
    return PendingAction(
        tenant_id=r["tenant_id"],
        conversation_id=r["conversation_id"],
        action_id=r["action_id"],
        capability=r["capability"],
        tool_name=r["tool_name"],
        request=CapabilityRequest.model_validate(r["request_json"]),
        args_hash=r["args_hash"],
        protected_fields=tuple(r["protected_fields"]),
        summary=r["summary"],
        created_from_turn=r["created_from_turn"],
        latest_prompt_outbox_id=r["latest_prompt_outbox_id"],
        confirmation_attempts=r["confirmation_attempts"],
        expires_at=r["expires_at"],
        status=PendingActionStatus(r["status"]),
    )


class _ActionRepo(_Repo):
    async def create_or_reuse(self, action: PendingAction) -> tuple[PendingAction, bool]:
        f = self._f
        stored = await self.get(action.action_id)
        if stored is not None:  # replay of the same turn: same deterministic action_id
            return stored, False
        awaiting = await self.awaiting()
        if awaiting is not None:
            if (awaiting.capability, awaiting.args_hash) == (action.capability, action.args_hash):
                return awaiting, False  # equivalent action already pending: reuse it
            # A different proposal replaces it: the old confirmation can never authorise the
            # new arguments (INV-010).
            await self.transition(awaiting.action_id, PendingActionStatus.INVALIDATED)
        now = self._clock.now()
        await self._c.execute(
            """
            INSERT INTO pending_actions (tenant_id, conversation_id, action_id, capability,
                tool_name, request_json, args_hash, protected_fields, summary, created_from_turn,
                latest_prompt_outbox_id, confirmation_attempts, expires_at, status,
                created_at, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,'PENDING_CONFIRMATION',$14,$14)
            """,
            f.tenant_id,
            f.conversation_id,
            action.action_id,
            action.capability,
            action.tool_name,
            action.request.model_dump(mode="json"),
            action.args_hash,
            list(action.protected_fields),
            action.summary,
            action.created_from_turn,
            action.latest_prompt_outbox_id,
            action.confirmation_attempts,
            action.expires_at,
            now,
        )
        created = await self.get(action.action_id)
        assert created is not None
        return created, True

    async def awaiting(self) -> PendingAction | None:
        r = await self._c.fetchrow(
            f"SELECT {_ACTION_COLUMNS} FROM pending_actions "
            "WHERE tenant_id=$1 AND conversation_id=$2 AND status='PENDING_CONFIRMATION'",
            self._f.tenant_id,
            self._f.conversation_id,
        )
        return _action_from_row(r) if r else None

    async def get(self, action_id: str) -> PendingAction | None:
        r = await self._c.fetchrow(
            f"SELECT {_ACTION_COLUMNS} FROM pending_actions WHERE tenant_id=$1 AND action_id=$2",
            self._f.tenant_id,
            action_id,
        )
        return _action_from_row(r) if r else None

    async def set_prompt(self, action_id: str, outbox_id: str, *, new_attempt: bool) -> None:
        await self._c.execute(
            "UPDATE pending_actions SET latest_prompt_outbox_id=$3, updated_at=$4, "
            "confirmation_attempts = confirmation_attempts + $5 "
            "WHERE tenant_id=$1 AND action_id=$2",
            self._f.tenant_id,
            action_id,
            outbox_id,
            self._clock.now(),
            1 if new_attempt else 0,
        )

    async def transition(
        self,
        action_id: str,
        to: PendingActionStatus,
        *,
        expected: PendingActionStatus = PendingActionStatus.PENDING_CONFIRMATION,
    ) -> bool:
        status = await self._c.execute(
            "UPDATE pending_actions SET status=$4, updated_at=$5 "
            "WHERE tenant_id=$1 AND action_id=$2 AND status=$3",
            self._f.tenant_id,
            action_id,
            expected.value,
            to.value,
            self._clock.now(),
        )
        return bool(status.endswith(" 1"))

    async def record_confirmation(self, confirmation: ActionConfirmation) -> None:
        await self._c.execute(
            """
            INSERT INTO action_confirmations (tenant_id, action_id, inbound_message_id,
                prompt_outbox_id, reply_to_provider_message_id, decision, interpreter,
                confidence, occurred_at, confirmed_at, created_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ON CONFLICT DO NOTHING
            """,
            self._f.tenant_id,
            confirmation.action_id,
            confirmation.inbound_message_id,
            confirmation.prompt_outbox_id,
            confirmation.reply_to_provider_message_id,
            confirmation.decision,
            confirmation.interpreter,
            confirmation.confidence,
            confirmation.occurred_at,
            confirmation.confirmed_at,
            self._clock.now(),
        )

    async def prompts(self, action_id: str) -> list[PromptRecord]:
        rows = await self._c.fetch(
            "SELECT outbox_id, status, provider_message_id, provider_accepted_at "
            "FROM outbox_messages WHERE tenant_id=$1 AND action_id=$2 "
            "ORDER BY created_at, outbox_id",
            self._f.tenant_id,
            action_id,
        )
        return [
            PromptRecord(
                outbox_id=r["outbox_id"],
                status=OutboxStatus(r["status"]),
                provider_message_id=r["provider_message_id"],
                provider_accepted_at=r["provider_accepted_at"],
            )
            for r in rows
        ]


class _OutboxRepo(_Repo):
    async def supersede(self, outbox_id: str) -> None:
        await self._c.execute(
            "UPDATE outbox_messages SET status='SUPERSEDED', updated_at=$3 "
            "WHERE tenant_id=$1 AND outbox_id=$2 AND status IN ('UNKNOWN', 'RECONCILING')",
            self._f.tenant_id,
            outbox_id,
            self._clock.now(),
        )

    async def add(self, message: OutboundMessage) -> bool:
        now = self._clock.now()
        status = await self._c.execute(
            """
            INSERT INTO outbox_messages (tenant_id, outbox_id, conversation_id, channel_id,
                contact_id, turn_id, message_index, action_id, text, idempotency_key, status,
                available_at, created_at, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'PENDING',$11,$11,$11)
            ON CONFLICT DO NOTHING
            """,
            message.tenant_id,
            message.outbox_id,
            message.conversation_id,
            message.channel_id,
            message.contact_id,
            message.turn_id,
            message.message_index,
            message.action_id,
            message.text,
            message.idempotency_key,
            now,
        )
        return bool(status.endswith(" 1"))  # "INSERT 0 1"


class _InboxRepo(_Repo):
    async def consume(self, event_ids: tuple[str, ...]) -> None:
        await self._set_status(event_ids, "CONSUMED")

    async def dead(self, event_ids: tuple[str, ...]) -> None:
        await self._set_status(event_ids, "DEAD")

    async def _set_status(self, event_ids: tuple[str, ...], status: str) -> None:
        await self._c.execute(
            "UPDATE inbox_events SET status=$4 WHERE tenant_id=$1 AND conversation_id=$2 "
            "AND event_id = ANY($3::text[]) AND status='CLAIMED'",
            self._f.tenant_id,
            self._f.conversation_id,
            list(event_ids),
            status,
        )


class PostgresUnitOfWork:
    def __init__(self, conn: asyncpg.Connection, tx: Any, fence: FenceToken, clock: Clock) -> None:
        self.fence = fence
        self._tx = tx
        self.committed = False
        self.state = _StateRepo(conn, fence, clock)
        self.turns = _TurnRepo(conn, fence, clock)
        self.journal = _JournalRepo(conn, fence, clock)
        self.invocations = _InvocationRepo(conn, fence, clock)
        self.actions = _ActionRepo(conn, fence, clock)
        self.outbox = _OutboxRepo(conn, fence, clock)
        self.inbox = _InboxRepo(conn, fence, clock)

    async def commit(self) -> None:
        await self._tx.commit()
        self.committed = True


class PostgresUnitOfWorkFactory:
    def __init__(
        self, db: PostgresDatabase, clock: Clock, coordination: CoordinationTime | None = None
    ) -> None:
        self._db = db
        self._clock = clock
        self._time = coordination or CoordinationTime(db, clock)

    @asynccontextmanager
    async def begin(self, fence: FenceToken) -> AsyncIterator[PostgresUnitOfWork]:
        async with self._db.pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            uow = PostgresUnitOfWork(conn, tx, fence, self._clock)
            try:
                # Owner + epoch + a lease that has NOT expired. A lease whose TTL elapsed is
                # stale even if nobody has taken over yet: its holder must stop writing.
                owned = await conn.fetchval(
                    "SELECT 1 FROM conversation_states WHERE tenant_id=$1 AND conversation_id=$2 "
                    "AND conversation_epoch=$3 AND lease_owner=$4 AND lease_expires_at > $5 "
                    "FOR SHARE",
                    fence.tenant_id,
                    fence.conversation_id,
                    fence.epoch,
                    fence.owner,
                    await self._time.now(conn),
                )
                if owned is None:
                    raise FencingError(
                        f"conversation {fence.conversation_id} is no longer owned by "
                        f"{fence.owner} at epoch {fence.epoch} (taken over or lease expired)"
                    )
                yield uow
            except BaseException:
                if not uow.committed:
                    await tx.rollback()
                raise
            if not uow.committed:
                await tx.rollback()  # nothing is persisted unless commit() was called


class PostgresTurnJournal:
    """`TurnJournal` port bound to one fence: every append is its own short fenced UoW."""

    def __init__(
        self, uow_factory: PostgresUnitOfWorkFactory, db: PostgresDatabase, fence: FenceToken
    ):
        self._factory = uow_factory
        self._db = db
        self._fence = fence

    async def get(self, turn_id: str, step_index: int) -> JournalEntry | None:
        r = await self._db.pool.fetchrow(
            f"SELECT {_JOURNAL_COLUMNS} FROM turn_journal "
            "WHERE tenant_id=$1 AND conversation_id=$2 AND turn_id=$3 AND step_index=$4",
            self._fence.tenant_id,
            self._fence.conversation_id,
            turn_id,
            step_index,
        )
        return _journal_from_row(r) if r else None

    async def append(self, entry: JournalEntry) -> None:
        async with self._factory.begin(self._fence) as uow:
            await uow.journal.append(entry)
            await uow.commit()

    async def entries(self, turn_id: str) -> list[JournalEntry]:
        rows = await self._db.pool.fetch(
            f"SELECT {_JOURNAL_COLUMNS} FROM turn_journal "
            "WHERE tenant_id=$1 AND conversation_id=$2 AND turn_id=$3 ORDER BY step_index",
            self._fence.tenant_id,
            self._fence.conversation_id,
            turn_id,
        )
        return [_journal_from_row(r) for r in rows]
