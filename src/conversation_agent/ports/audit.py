from __future__ import annotations

from typing import Protocol

from conversation_agent.core.models.audit import AuditEntry, AuditRecord


class AuditLog(Protocol):
    """Append-only. `record` is part of the operation it audits: a failure to record fails the
    operation (an administrative action with no trail is not allowed to happen)."""

    async def record(self, entry: AuditEntry) -> None: ...

    async def list(
        self,
        tenant_id: str,
        *,
        action: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditRecord]:
        """Newest first, one tenant only."""
        ...
