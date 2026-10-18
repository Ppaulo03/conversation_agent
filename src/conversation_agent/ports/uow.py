from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from conversation_agent.core.models.actions import (
    ActionConfirmation,
    PendingAction,
    PendingActionStatus,
    PromptRecord,
)
from conversation_agent.core.models.audit import AuditEntry
from conversation_agent.core.models.conversation import ConversationIdentity, ConversationState
from conversation_agent.core.models.journal import JournalEntry, JournalStepType
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
    agent_id: str | None = None  # the agent this conversation is pinned to (None: legacy row)
    agent_version: str | None = None  # ... and its published version


class ConversationStateRepository(Protocol):
    async def load(self) -> StoredConversation: ...
    async def save(self, state: ConversationState, *, last_event_at: datetime | None) -> None: ...
    async def pin_agent(self, agent_id: str, version: str) -> None: ...
    async def set_ownership(self, ownership: Ownership) -> None:
        """Fenced like every other write: only the lease owner can move ownership."""
        ...


class TurnRepository(Protocol):
    async def open_next(self, owner: str, now: datetime) -> OpenedTurn | None:
        """Resume the conversation's open turn, or claim READY events into a new one."""
        ...

    async def record_failure(self, turn_id: str) -> int:
        """Counts a genuine failure (not a wait for an external result); returns the total."""
        ...

    async def defer(self, turn_id: str, retry_at: datetime) -> None:
        """Keeps an open turn durable but invisible to workers until `retry_at`."""
        ...

    async def complete(self, turn_id: str) -> None: ...
    async def cancel(self, turn_id: str) -> None:
        """Abandon a turn (no irreversible effect yet); its events return to READY."""
        ...

    async def fail(self, turn_id: str, reason: str) -> None: ...


class JournalRepository(Protocol):
    async def get(self, turn_id: str, step_index: int) -> JournalEntry | None: ...
    async def find_step(self, turn_id: str, step_type: JournalStepType) -> JournalEntry | None: ...
    async def append(self, entry: JournalEntry) -> None: ...


class InvocationRepository(Protocol):
    async def create_prepared(self, invocation: ToolInvocation) -> ToolInvocation:
        """Idempotent on (tenant_id, invocation_id): returns the existing row if present."""
        ...

    async def get(self, invocation_id: str) -> ToolInvocation | None: ...
    async def for_turn(self, turn_id: str) -> list[ToolInvocation]: ...
    async def mark_applied(self, invocation_id: str, now: datetime) -> None: ...


class ActionRepository(Protocol):
    """PendingAction / ActionConfirmation, all inside the fenced local transaction."""

    async def create_or_reuse(self, action: PendingAction) -> tuple[PendingAction, bool]:
        """Idempotent on action_id. An equivalent awaiting action (same capability + args_hash)
        is reused; a different awaiting one is INVALIDATED first (INV-010). Returns
        (stored action, created_new)."""
        ...

    async def awaiting(self) -> PendingAction | None: ...
    async def get(self, action_id: str) -> PendingAction | None: ...
    async def set_prompt(self, action_id: str, outbox_id: str, *, new_attempt: bool) -> None: ...
    async def transition(
        self,
        action_id: str,
        to: PendingActionStatus,
        *,
        expected: PendingActionStatus = PendingActionStatus.PENDING_CONFIRMATION,
    ) -> bool:
        """Compare-and-set on the current status. False if it was not `expected`."""
        ...

    async def record_confirmation(self, confirmation: ActionConfirmation) -> None: ...
    async def prompts(self, action_id: str) -> list[PromptRecord]: ...


class OutboxRepository(Protocol):
    async def add(self, message: OutboundMessage) -> bool:
        """Idempotent on (tenant, conversation, turn, message_index). False if it existed."""
        ...

    async def supersede(self, outbox_id: str) -> None:
        """An UNKNOWN prompt that was replaced by a new one: no longer eligible (SUPERSEDED)."""
        ...


class InboxRepository(Protocol):
    async def consume(self, event_ids: tuple[str, ...]) -> None: ...
    async def dead(self, event_ids: tuple[str, ...]) -> None: ...


class AuditRepository(Protocol):
    async def record(self, entry: AuditEntry) -> None:
        """Writes the entry IN THIS TRANSACTION: it commits or rolls back with the change it
        describes (an administrative action with no trail does not happen)."""
        ...


class ConversationUnitOfWork(Protocol):
    """One local transaction, valid only while `fence` still owns the conversation (INV-009).

    Never held open across LLM or external I/O. `commit()` is the only way changes persist.
    """

    fence: FenceToken
    state: ConversationStateRepository
    turns: TurnRepository
    journal: JournalRepository
    invocations: InvocationRepository
    actions: ActionRepository
    outbox: OutboxRepository
    inbox: InboxRepository
    audit: AuditRepository

    async def commit(self) -> None: ...


class ConversationUnitOfWorkFactory(Protocol):
    def begin(self, fence: FenceToken) -> AbstractAsyncContextManager[ConversationUnitOfWork]:
        """Raises `FencingError` if the fence is not the current owner/epoch."""
        ...
