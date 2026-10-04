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


class ConfirmationConflictError(ConversationAgentError):
    """The pending action changed (or the conversation left the bot) while confirming:
    nothing is confirmed and nothing is prepared."""


class ConnectionNotFoundError(ConversationAgentError):
    """The tenant has no connection with that id (a configuration problem)."""


class SecretNotFoundError(ConversationAgentError):
    """The secret reference could not be resolved (message never contains secret material)."""


class FencingError(ConversationAgentError):
    """The conversation lease/epoch is no longer ours: no conversational mutation may commit
    (INV-009)."""


class ExecutionFencingError(ConversationAgentError):
    """The invocation was taken over by another executor (stale `execution_epoch`) (INV-016)."""


class StaleWorkerError(ConversationAgentError):
    """Heartbeat lost / lease expired locally: stop at the next safe boundary (no new steps)."""


class TurnCancelledError(ConversationAgentError):
    """A newer message asked to restart the turn and nothing irreversible had happened yet:
    the turn is abandoned at a safe boundary and its events go back to READY (DESIGN 20.2)."""


class ToolResultPendingError(ConversationAgentError):
    """The tool outcome is not known yet (executing elsewhere or under reconciliation).
    The turn stays open and is resumed later; nothing is re-executed."""


class PublishError(ConversationAgentError):
    """A published version is immutable and versions only move forward (Phase 5)."""


class VersionConflictError(PublishError):
    """That (agent, version) already exists with DIFFERENT content."""


class VersionRegressionError(PublishError):
    """A new version must be greater than the latest published one."""


class IncompatibleUpgradeError(PublishError):
    """Breaking changes (removed capability, lower risk, changed schemas) need a MAJOR bump."""


class AgentVersionUnavailableError(ConversationAgentError):
    """A pinned agent version is not in the registry: the runtime must not guess another."""


class RegistryIntegrityError(ConversationAgentError):
    """A stored agent no longer compiles to the digest it was published with."""


class AgentMismatchError(ConversationAgentError):
    """The conversation is pinned to another agent and still has something in progress: the
    runtime never silently swaps the agent under a flow or a pending confirmation."""


class RegistryCompatibilityError(RegistryIntegrityError):
    """A stored agent was published with a compiler/manifest format this build cannot load.
    Loading it with today's compiler would reinterpret history, so it is refused explicitly."""
