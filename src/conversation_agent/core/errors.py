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
