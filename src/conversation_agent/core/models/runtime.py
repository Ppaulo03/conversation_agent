"""Runtime-protocol models: leases, fences, inbox/outbox rows, tool-invocation ledger.

These describe *conversational/operational* state only (INV-008). The statuses mirror the
state machines in DESIGN §39B.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from conversation_agent.core.models.conversation import ConversationIdentity
from conversation_agent.core.models.tooling import CapabilityRequest, ToolContext


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ConversationKey(_Frozen):
    tenant_id: str
    conversation_id: str


class FenceToken(_Frozen):
    """Proof of conversation ownership: `owner` held `epoch` when the work started (INV-009)."""

    tenant_id: str
    conversation_id: str
    owner: str
    epoch: int

    @property
    def key(self) -> ConversationKey:
        return ConversationKey(tenant_id=self.tenant_id, conversation_id=self.conversation_id)


class Lease(_Frozen):
    key: ConversationKey
    owner: str
    epoch: int
    expires_at: AwareDatetime

    @property
    def fence(self) -> FenceToken:
        return FenceToken(
            tenant_id=self.key.tenant_id,
            conversation_id=self.key.conversation_id,
            owner=self.owner,
            epoch=self.epoch,
        )


class Ownership(StrEnum):
    BOT = "BOT"
    HANDOFF_PENDING = "HANDOFF_PENDING"
    HUMAN = "HUMAN"


# --- Inbox ----------------------------------------------------------------------------------


class InboundEvent(_Frozen):
    """A normalised inbound event. (tenant_id, channel_id, event_id) is its dedupe identity."""

    tenant_id: str
    channel_id: str
    event_id: str
    conversation_id: str
    contact_id: str
    session_id: str
    text: str
    occurred_at: AwareDatetime
    received_at: AwareDatetime
    source_sequence: int | None = None

    @property
    def identity(self) -> ConversationIdentity:
        return ConversationIdentity(
            tenant_id=self.tenant_id,
            channel_id=self.channel_id,
            conversation_id=self.conversation_id,
            session_id=self.session_id,
            contact_id=self.contact_id,
        )


class OpenedTurn(_Frozen):
    """A turn whose events were claimed under the conversation lease."""

    turn_id: str
    identity: ConversationIdentity
    user_text: str
    event_ids: tuple[str, ...]
    late_event_ids: tuple[str, ...] = ()
    last_event_at: AwareDatetime | None = None  # newest occurred_at among the turn's events
    resumed: bool = False  # True when an earlier owner had already opened this turn
    attempts: int = 0  # failed processing attempts so far (NOT waits for tool results)


# --- Outbox ---------------------------------------------------------------------------------


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    QUEUED = "QUEUED"
    ACCEPTED = "ACCEPTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    SUPERSEDED = "SUPERSEDED"


class OutboundMessage(_Frozen):
    outbox_id: str
    tenant_id: str
    conversation_id: str
    channel_id: str
    contact_id: str
    turn_id: str
    message_index: int
    text: str
    idempotency_key: str
    status: OutboxStatus = OutboxStatus.PENDING
    action_id: str | None = None
    attempts: int = 0
    provider_message_id: str | None = None


class SendResult(_Frozen):
    """What a MessageSender reports. ACCEPTED means accepted for sending, not delivered/read."""

    status: OutboxStatus  # QUEUED | ACCEPTED | FAILED | UNKNOWN
    provider_message_id: str | None = None
    retryable: bool = False


# --- Tool-invocation ledger -----------------------------------------------------------------


class InvocationStatus(StrEnum):
    PREPARED = "PREPARED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    RECONCILED = "RECONCILED"
    HUMAN_HANDOFF = "HUMAN_HANDOFF"


TERMINAL_INVOCATION_STATUSES = frozenset(
    {
        InvocationStatus.SUCCEEDED,
        InvocationStatus.FAILED,
        InvocationStatus.RECONCILED,
        InvocationStatus.HUMAN_HANDOFF,
    }
)


class ApplicationStatus(StrEnum):
    NONE = "none"  # no terminal fact yet
    PENDING = "pending"  # fact recorded (C1), not yet applied to the conversation
    APPLIED = "applied"  # applied (C2)


class ToolInvocation(_Frozen):
    """One external-operation attempt. Identity is runtime-made, never LLM-made (§9.1)."""

    tenant_id: str
    invocation_id: str
    conversation_id: str
    session_id: str
    turn_id: str
    logical_step_id: str
    attempt_semantic_id: str
    action_id: str | None = None
    tool_name: str
    capability: str
    args_hash: str
    idempotency_key: str
    request: CapabilityRequest
    context: ToolContext
    status: InvocationStatus = InvocationStatus.PREPARED
    execution_owner: str | None = None
    execution_lease_expires_at: datetime | None = None
    execution_epoch: int = 0
    reconcile_attempts: int = 0
    result_application_status: ApplicationStatus = ApplicationStatus.NONE
    result: dict[str, Any] | None = None  # serialised ToolResult
    error: dict[str, Any] | None = None
    provider_metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionClaim(_Frozen):
    invocation_id: str
    epoch: int


# --- Scheduler ------------------------------------------------------------------------------


class ScheduledEvent(_Frozen):
    tenant_id: str
    scheduler_key: str
    event_type: str
    due_at: AwareDatetime
    payload: dict[str, Any] = Field(default_factory=dict)
