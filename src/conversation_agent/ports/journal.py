from __future__ import annotations

from typing import Protocol

from conversation_agent.core.models.journal import JournalEntry


class TurnJournal(Protocol):
    """Append-only journal. Identity of a step is (turn_id, step_index, step_type)."""

    async def get(self, turn_id: str, step_index: int) -> JournalEntry | None: ...

    async def append(self, entry: JournalEntry) -> None:
        """Raises `JournalConflictError` if (turn_id, step_index) is already occupied."""
        ...

    async def entries(self, turn_id: str) -> list[JournalEntry]: ...
