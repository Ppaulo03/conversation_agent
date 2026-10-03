from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from conversation_agent.core.models.conversation import ConversationIdentity, ConversationState
from conversation_agent.core.models.journal import JournalEntry
from conversation_agent.core.models.runtime import (
    FenceToken,
    OpenedTurn,
    OutboundMessage,
    Ownership,
    ToolInvocation,
)


class StoredConversation(BaseModel):
    model_config = ConfigDict(frozen=True)

    identity: ConversationIdentity
    state: ConversationState
    ownership: Ownership
    version: int
    last_event_at: datetime | None  # watermark: events at/before it are "late" for new turns


class ConversationStateRepository(Protocol):
    async def load(self) -> StoredConversation: ...
    async def save(self, state: ConversationState, *, last_event_at: datetime | None) -> None: ...


class TurnRepository(Protocol):
    async def open_next(self, owner: str, now: datetime) -> OpenedTurn | None:
        """Resume the conversation's open turn, or claim READY events into a new one."""
        ...

    async def record_failure(self, turn_id: str) -> int:
        """Counts a genuine failure (not a wait for an external result); returns the total."""
        ...

    async def complete(self, turn_id: str) -> None: ...
    async def fail(self, turn_id: str, reason: str) -> None: ...


class JournalRepository(Protocol):
    async def get(self, turn_id: str, step_index: int) -> JournalEntry | None: ...
    async def append(self, entry: JournalEntry) -> None: ...


class InvocationRepository(Protocol):
    async def create_prepared(self, invocation: ToolInvocation) -> ToolInvocation:
        """Idempotent on (tenant_id, invocation_id): returns the existing row if present."""
        ...

    async def get(self, invocation_id: str) -> ToolInvocation | None: ...
    async def for_turn(self, turn_id: str) -> list[ToolInvocation]: ...
    async def mark_applied(self, invocation_id: str, now: datetime) -> None: ...


class OutboxRepository(Protocol):
    async def add(self, message: OutboundMessage) -> bool:
        """Idempotent on (tenant, conversation, turn, message_index). False if it existed."""
        ...


class InboxRepository(Protocol):
    async def consume(self, event_ids: tuple[str, ...]) -> None: ...
    async def dead(self, event_ids: tuple[str, ...]) -> None: ...


class ConversationUnitOfWork(Protocol):
    """One local transaction, valid only while `fence` still owns the conversation (INV-009).

    Never held open across LLM or external I/O. `commit()` is the only way changes persist.
    """

    fence: FenceToken
    state: ConversationStateRepository
    turns: TurnRepository
    journal: JournalRepository
    invocations: InvocationRepository
    outbox: OutboxRepository
    inbox: InboxRepository

    async def commit(self) -> None: ...


class ConversationUnitOfWorkFactory(Protocol):
    def begin(self, fence: FenceToken) -> AbstractAsyncContextManager[ConversationUnitOfWork]:
        """Raises `FencingError` if the fence is not the current owner/epoch."""
        ...
