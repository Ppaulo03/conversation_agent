"""What the runtime can say about its own health, read from the durable state (Phase 10).

Every number here is a BACKLOG or an AGE: how many things wait, and how long the oldest one has
waited. They are the honest signals of this architecture (everything is queued in PostgreSQL), they
need no instrumentation inside the hot path, and they are the same whichever worker answers.
Ages use the database clock (INV-032).
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from conversation_agent.adapters.postgres.db import PostgresDatabase


@dataclass(frozen=True)
class HealthSnapshot:
    inbox_ready: int = 0  # inbound events waiting for a turn
    inbox_oldest_ready_age_seconds: float = 0.0
    turns_processing: int = 0
    turns_oldest_processing_age_seconds: float = 0.0
    outbox_pending: int = 0  # replies waiting to be sent
    outbox_oldest_pending_age_seconds: float = 0.0
    outbox_unsettled: int = 0  # sent but not confirmed (queued / unknown / reconciling)
    outbox_oldest_unsettled_age_seconds: float = 0.0
    invocations_unresolved: int = 0  # external effects whose outcome is not known yet
    invocations_oldest_unresolved_age_seconds: float = 0.0
    invocations_human_handoff: int = 0  # effects waiting for a person
    scheduled_overdue: int = 0  # timers past their due time and not done
    scheduled_oldest_overdue_age_seconds: float = 0.0
    handoffs_pending: int = 0  # conversations waiting for a human to take them
    handoffs_oldest_pending_age_seconds: float = 0.0
    pending_confirmations: int = 0  # protected actions waiting for the contact's answer

    def as_dict(self) -> dict[str, float]:
        return {f.name: float(getattr(self, f.name)) for f in fields(self)}


_AGE = "COALESCE(EXTRACT(EPOCH FROM clock_timestamp() - min({column}))::float8, 0)"


async def _pair(conn: Any, table: str, where: str, column: str) -> tuple[int, float]:
    row = await conn.fetchrow(
        f"SELECT count(*) AS n, {_AGE.format(column=column)} AS age FROM {table} WHERE {where}"
    )
    return int(row["n"]), float(row["age"])


async def collect_health(db: PostgresDatabase) -> HealthSnapshot:
    async with db.pool.acquire() as conn:
        inbox = await _pair(conn, "inbox_events", "status = 'READY'", "received_at")
        turns = await _pair(conn, "turns", "status = 'PROCESSING'", "created_at")
        pending = await _pair(
            conn, "outbox_messages", "status = 'PENDING' AND available_at <= clock_timestamp()",
            "available_at",
        )  # fmt: skip
        unsettled = await _pair(
            conn, "outbox_messages", "status IN ('QUEUED', 'UNKNOWN', 'RECONCILING')", "updated_at"
        )
        unresolved = await _pair(
            conn, "tool_invocations", "status IN ('UNKNOWN', 'RECONCILING', 'EXECUTING')",
            "prepared_at",
        )  # fmt: skip
        overdue = await _pair(
            conn, "scheduled_events",
            "status IN ('PENDING', 'CLAIMED') AND due_at < clock_timestamp()", "due_at",
        )  # fmt: skip
        handoffs = await _pair(
            conn, "conversation_states", "ownership = 'HANDOFF_PENDING'", "updated_at"
        )
        human = await conn.fetchval(
            "SELECT count(*) FROM tool_invocations WHERE status = 'HUMAN_HANDOFF'"
        )
        confirmations = await conn.fetchval(
            "SELECT count(*) FROM pending_actions WHERE status = 'PENDING_CONFIRMATION'"
        )
    return HealthSnapshot(
        inbox_ready=inbox[0],
        inbox_oldest_ready_age_seconds=inbox[1],
        turns_processing=turns[0],
        turns_oldest_processing_age_seconds=turns[1],
        outbox_pending=pending[0],
        outbox_oldest_pending_age_seconds=pending[1],
        outbox_unsettled=unsettled[0],
        outbox_oldest_unsettled_age_seconds=unsettled[1],
        invocations_unresolved=unresolved[0],
        invocations_oldest_unresolved_age_seconds=unresolved[1],
        invocations_human_handoff=int(human),
        scheduled_overdue=overdue[0],
        scheduled_oldest_overdue_age_seconds=overdue[1],
        handoffs_pending=handoffs[0],
        handoffs_oldest_pending_age_seconds=handoffs[1],
        pending_confirmations=int(confirmations),
    )
