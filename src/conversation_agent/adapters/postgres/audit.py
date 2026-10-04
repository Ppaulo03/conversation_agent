from __future__ import annotations

from typing import Any

from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.models.audit import AuditEntry, AuditRecord

_COLUMNS = "id, tenant_id, occurred_at, actor, action, subject_type, subject_id, outcome, details"


async def insert_audit(conn: Any, entry: AuditEntry) -> None:
    """Writes one entry on `conn`: call it inside the transaction of the operation it audits, so
    the action and its trail commit or roll back together."""
    await conn.execute(
        "INSERT INTO admin_audit (tenant_id, actor, action, subject_type, subject_id, "
        "outcome, details) VALUES ($1,$2,$3,$4,$5,$6,$7)",
        entry.tenant_id,
        entry.actor,
        entry.action,
        entry.subject_type,
        entry.subject_id,
        entry.outcome,
        entry.details,
    )


class PostgresAuditLog:
    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db

    async def record(self, entry: AuditEntry) -> None:
        async with self._db.pool.acquire() as conn:
            await insert_audit(conn, entry)

    async def list(
        self,
        tenant_id: str,
        *,
        action: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditRecord]:
        rows = await self._db.pool.fetch(
            f"SELECT {_COLUMNS} FROM admin_audit WHERE tenant_id = $1 "
            "AND ($2::text IS NULL OR action = $2) "
            "AND ($3::text IS NULL OR subject_type = $3) "
            "AND ($4::text IS NULL OR subject_id = $4) "
            "ORDER BY id DESC LIMIT $5",
            tenant_id,
            action,
            subject_type,
            subject_id,
            min(max(limit, 1), 1000),
        )
        return [AuditRecord.model_validate(dict(r)) for r in rows]
