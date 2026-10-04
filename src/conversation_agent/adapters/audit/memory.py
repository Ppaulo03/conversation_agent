from __future__ import annotations

from datetime import UTC, datetime

from conversation_agent.core.models.audit import AuditEntry, AuditRecord


class InMemoryAuditLog:
    """Same contract as the durable log, for tests and single-process tooling."""

    def __init__(self) -> None:
        self._records: list[AuditRecord] = []

    async def record(self, entry: AuditEntry) -> None:
        self._records.append(
            AuditRecord(
                **entry.model_dump(), id=len(self._records) + 1, occurred_at=datetime.now(UTC)
            )
        )

    async def list(
        self,
        tenant_id: str,
        *,
        action: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditRecord]:
        found = [
            r
            for r in reversed(self._records)
            if r.tenant_id == tenant_id
            and (action is None or r.action == action)
            and (subject_type is None or r.subject_type == subject_type)
            and (subject_id is None or r.subject_id == subject_id)
        ]
        return found[: min(max(limit, 1), 1000)]
