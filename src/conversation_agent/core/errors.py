"""Typed framework errors. No provider-specific exception crosses into the engine."""

from __future__ import annotations


class ConversationAgentError(Exception):
    """Base class for framework errors."""


class LLMProviderError(ConversationAgentError):
    """Raised by LLM adapters in place of any SDK-specific exception."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class JournalDivergenceError(ConversationAgentError):
    """Replay found a journal step whose type or request_hash differs (INV-017).

    Fail-closed: the turn must abort; nothing is silently re-executed.
    """


class JournalConflictError(ConversationAgentError):
    """Append on an already-occupied (turn_id, step_index)."""


class MappingError(ConversationAgentError):
    """An input/output mapping could not be applied."""


class DefinitionError(ConversationAgentError):
    """Inconsistent agent/capability/binding definitions."""


class ConversationIdentityConflictError(ConversationAgentError):
    """An event reused a conversation_id with a different channel or contact.

    `conversation_id` is unique within a tenant and bound to one channel and one contact;
    reusing it elsewhere would mix two people's state, so the event is refused."""


class FencingError(ConversationAgentError):
    """The conversation lease/epoch is no longer ours: no conversational mutation may commit
    (INV-009)."""


class ExecutionFencingError(ConversationAgentError):
    """The invocation was taken over by another executor (stale `execution_epoch`) (INV-016)."""


class StaleWorkerError(ConversationAgentError):
    """Heartbeat lost / lease expired locally: stop at the next safe boundary (no new steps)."""


class ToolResultPendingError(ConversationAgentError):
    """The tool outcome is not known yet (executing elsewhere or under reconciliation).
    The turn stays open and is resumed later; nothing is re-executed."""
