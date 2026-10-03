from __future__ import annotations

from conversation_agent.core.errors import JournalConflictError
from conversation_agent.core.models.journal import JournalEntry


class InMemoryTurnJournal:
    """Local, non-durable journal for the vertical slice and replay tests.

    The durable PostgreSQL journal (UNIQUE(turn, step_index), epoch fencing) is Phase 2.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, int], JournalEntry] = {}

    async def get(self, turn_id: str, step_index: int) -> JournalEntry | None:
        return self._entries.get((turn_id, step_index))

    async def append(self, entry: JournalEntry) -> None:
        key = (entry.turn_id, entry.step_index)
        if key in self._entries:
            raise JournalConflictError(f"journal slot already taken: {key}")
        self._entries[key] = entry

    async def entries(self, turn_id: str) -> list[JournalEntry]:
        return sorted(
            (e for (t, _), e in self._entries.items() if t == turn_id), key=lambda e: e.step_index
        )
