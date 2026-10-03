"""ToolInvocation ledger over PostgreSQL.

Every transition after external I/O is a compare-and-set on `execution_epoch` and never looks
at the conversation lease (INV-011, INV-016): losing the conversation does not erase an
external fact. Applying a fact to the conversation is a different transaction (the UoW).

`result_application_status` becomes `pending` only for *terminal* facts. An `UNKNOWN` outcome
is recorded (status + error) but waits for reconciliation before it may be applied.
"""

from __future__ import annotations

from datetime import timedelta

import asyncpg

from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.rows import INVOCATION_COLUMNS, invocation_from_row
from conversation_agent.core.errors import ExecutionFencingError
from conversation_agent.core.models.runtime import ExecutionClaim, ToolInvocation
from conversation_agent.core.models.tooling import ToolResult
from conversation_agent.ports.clock import Clock

_SELECT = f"SELECT {INVOCATION_COLUMNS} FROM tool_invocations"
_RETURNING = f"RETURNING {INVOCATION_COLUMNS}"
_T_COLUMNS = ", ".join(f"t.{c.strip()}" for c in INVOCATION_COLUMNS.split(","))


def _ledger_status(result: ToolResult) -> str:
    """success -> SUCCEEDED, unknown -> UNKNOWN, any other known outcome -> FAILED."""
    if result.status == "success":
        return "SUCCEEDED"
    if result.status == "unknown":
        return "UNKNOWN"
    return "FAILED"


class PostgresToolInvocationStore:
    def __init__(self, db: PostgresDatabase, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    async def get(self, tenant_id: str, invocation_id: str) -> ToolInvocation | None:
        r = await self._db.pool.fetchrow(
            f"{_SELECT} WHERE tenant_id=$1 AND invocation_id=$2", tenant_id, invocation_id
        )
        return invocation_from_row(r) if r else None

    async def claim_execution(
        self, tenant_id: str, invocation_id: str, owner: str, ttl: timedelta
    ) -> ExecutionClaim | None:
        now = self._clock.now()
        row = await self._db.pool.fetchrow(
            """
            UPDATE tool_invocations
               SET status = 'EXECUTING', execution_owner = $3, execution_lease_expires_at = $4,
                   execution_epoch = execution_epoch + 1, started_at = COALESCE(started_at, $5)
             WHERE tenant_id = $1 AND invocation_id = $2 AND status = 'PREPARED'
            RETURNING execution_epoch
            """,
            tenant_id,
            invocation_id,
            owner,
            now + ttl,
            now,
        )
        return (
            ExecutionClaim(invocation_id=invocation_id, epoch=row["execution_epoch"])
            if row
            else None
        )

    async def renew_execution(
        self, tenant_id: str, invocation_id: str, claim: ExecutionClaim, ttl: timedelta
    ) -> bool:
        row = await self._db.pool.fetchrow(
            """
            UPDATE tool_invocations SET execution_lease_expires_at = $4
             WHERE tenant_id = $1 AND invocation_id = $2 AND execution_epoch = $3
               AND status IN ('EXECUTING', 'RECONCILING')
            RETURNING 1
            """,
            tenant_id,
            invocation_id,
            claim.epoch,
            self._clock.now() + ttl,
        )
        return row is not None

    async def finalize_execution(
        self, tenant_id: str, invocation_id: str, claim: ExecutionClaim, result: ToolResult
    ) -> ToolInvocation:
        """C1. Records the external fact; independent of the conversation lease."""
        status = _ledger_status(result)
        application = "none" if status == "UNKNOWN" else "pending"
        row = await self._db.pool.fetchrow(
            f"""
            UPDATE tool_invocations
               SET status = $4, result_json = $5, error_json = $6, provider_metadata = $7,
                   result_application_status = $8, finished_at = $9,
                   execution_lease_expires_at = NULL
             WHERE tenant_id = $1 AND invocation_id = $2
               AND status = 'EXECUTING' AND execution_epoch = $3
            {_RETURNING}
            """,
            tenant_id,
            invocation_id,
            claim.epoch,
            status,
            result.model_dump(mode="json"),
            result.error.model_dump(mode="json") if result.error else None,
            result.provider_metadata,
            application,
            self._clock.now(),
        )
        if row is None:
            raise ExecutionFencingError(
                f"invocation {invocation_id} is no longer EXECUTING at epoch {claim.epoch}"
            )
        return invocation_from_row(row)

    async def claim_reconciliation(
        self, owner: str, limit: int, ttl: timedelta
    ) -> list[tuple[ToolInvocation, ExecutionClaim]]:
        """UNKNOWN, expired-EXECUTING and expired-RECONCILING invocations -> RECONCILING under a
        *new* execution_epoch. An expired EXECUTING is never presumed failed (DESIGN §14), and
        the previous executor is fenced out the moment its epoch is superseded."""
        now = self._clock.now()
        rows = await self._db.pool.fetch(
            f"""
            WITH picked AS (
                SELECT tenant_id, invocation_id FROM tool_invocations
                 WHERE (status = 'UNKNOWN' AND NOT EXISTS (
                           SELECT 1 FROM scheduled_events s
                            WHERE s.tenant_id = tool_invocations.tenant_id
                              AND s.scheduler_key = 'reconcile:' || tool_invocations.invocation_id
                              AND s.status IN ('PENDING', 'CLAIMED')))  -- backoff timer pending
                    OR (status IN ('EXECUTING', 'RECONCILING') AND execution_lease_expires_at <= $1)
                 ORDER BY prepared_at
                 LIMIT $2 FOR UPDATE SKIP LOCKED
            )
            UPDATE tool_invocations t
               SET status = 'RECONCILING', execution_owner = $3, execution_lease_expires_at = $4,
                   execution_epoch = t.execution_epoch + 1,
                   reconcile_attempts = t.reconcile_attempts + 1
              FROM picked
             WHERE t.tenant_id = picked.tenant_id AND t.invocation_id = picked.invocation_id
            RETURNING {_T_COLUMNS}
            """,
            now,
            limit,
            owner,
            now + ttl,
        )
        return [
            (
                invocation_from_row(r),
                ExecutionClaim(invocation_id=r["invocation_id"], epoch=r["execution_epoch"]),
            )
            for r in rows
        ]

    async def finalize_reconciliation(
        self,
        tenant_id: str,
        invocation_id: str,
        claim: ExecutionClaim,
        result: ToolResult | None,
        *,
        handoff: bool = False,
    ) -> ToolInvocation:
        now = self._clock.now()
        if handoff:
            status, application = "HUMAN_HANDOFF", "pending"
        elif result is None or result.status == "unknown":
            status, application = "UNKNOWN", "none"  # still unresolved: try again later
        elif result.status == "success":
            status, application = "RECONCILED", "pending"
        else:
            status, application = "FAILED", "pending"
        row = await self._db.pool.fetchrow(
            f"""
            UPDATE tool_invocations
               SET status = $4, result_json = COALESCE($5, result_json),
                   error_json = COALESCE($6, error_json), result_application_status = $7,
                   finished_at = CASE WHEN $7 = 'pending' THEN $8 ELSE finished_at END,
                   execution_lease_expires_at = NULL
             WHERE tenant_id = $1 AND invocation_id = $2
               AND status = 'RECONCILING' AND execution_epoch = $3
            {_RETURNING}
            """,
            tenant_id,
            invocation_id,
            claim.epoch,
            status,
            result.model_dump(mode="json") if result else None,
            result.error.model_dump(mode="json") if result and result.error else None,
            application,
            now,
        )
        if row is None:
            raise ExecutionFencingError(
                f"reconciliation of {invocation_id} lost its claim (epoch {claim.epoch})"
            )
        return invocation_from_row(row)

    async def pending_application(
        self, tenant_id: str, conversation_id: str
    ) -> list[ToolInvocation]:
        rows: list[asyncpg.Record] = await self._db.pool.fetch(
            f"{_SELECT} WHERE tenant_id=$1 AND conversation_id=$2 "
            "AND result_application_status='pending' ORDER BY prepared_at",
            tenant_id,
            conversation_id,
        )
        return [invocation_from_row(r) for r in rows]
