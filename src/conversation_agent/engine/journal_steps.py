"""Journal-driven step execution with fail-closed replay (INV-014, INV-017)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from conversation_agent.core.errors import JournalDivergenceError
from conversation_agent.core.models.journal import JournalEntry, JournalStepType
from conversation_agent.ports.journal import TurnJournal


class TurnJournalCursor:
    """Walks the deterministic step sequence of one turn.

    For each step: if the journal already holds (turn_id, step_index) it must have the
    same step_type and request_hash, and its persisted payload is reused without calling
    `produce`; otherwise `produce` runs once and its payload is appended.
    """

    def __init__(self, journal: TurnJournal, turn_id: str) -> None:
        self._journal = journal
        self._turn_id = turn_id
        self._index = 0

    @property
    def next_index(self) -> int:
        return self._index

    async def step(
        self,
        step_type: JournalStepType,
        request_hash: str | None,
        produce: Callable[[], Awaitable[dict[str, Any]]],
        *,
        logical_step_id: str | None = None,
    ) -> dict[str, Any]:
        index = self._index
        self._index += 1
        stored = await self._journal.get(self._turn_id, index)
        if stored is not None:
            if stored.step_type != step_type or stored.request_hash != request_hash:
                raise JournalDivergenceError(
                    f"journal divergence at turn={self._turn_id} step={index}: "
                    f"stored=({stored.step_type}, {stored.request_hash}) "
                    f"expected=({step_type}, {request_hash})"
                )
            return stored.payload
        payload = await produce()
        await self._journal.append(
            JournalEntry(
                turn_id=self._turn_id,
                step_index=index,
                step_type=step_type,
                request_hash=request_hash,
                logical_step_id=logical_step_id,
                payload=payload,
            )
        )
        return payload
