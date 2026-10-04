"""Runtime-protocol models: leases, fences, inbox/outbox rows, tool-invocation ledger.

These describe *conversational/operational* state only (INV-008). The statuses mirror the
state machines in DESIGN §39B.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from conversation_agent.core.definitions.mapping import MappingSpec
from conversation_agent.core.models.conversation import ConversationIdentity
from conversation_agent.core.models.media import MediaReference
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
    observed_at: AwareDatetime | None = None  # authority time when this lease was granted/renewed
    cancel_requested: bool = False  # a newer message asked to restart (read with the heartbeat)

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
    media: tuple[MediaReference, ...] = ()  # claim-check references, never bytes
    provider_message_id: str | None = None  # the provider's id of the message this event carries
    # "system": a runtime-originated event (e.g. a proactive timer), not something the contact wrote
    kind: Literal["user", "system"] = "user"
    # Channel-domain evidence used to prove a reply belongs to a confirmation prompt (DESIGN §10):
    provider_occurred_at: AwareDatetime | None = None  # comparable with `provider_accepted_at`
    reply_to_provider_message_id: str | None = None

    @property
    def identity(self) -> ConversationIdentity:
        return ConversationIdentity(
            tenant_id=self.tenant_id,
            channel_id=self.channel_id,
            conversation_id=self.conversation_id,
            session_id=self.session_id,
            contact_id=self.contact_id,
        )


class InboundRef(_Frozen):
    event_id: str
    provider_occurred_at: AwareDatetime | None = None
    reply_to_provider_message_id: str | None = None


class OpenedTurn(_Frozen):
    """A turn whose events were claimed under the conversation lease."""

    turn_id: str
    identity: ConversationIdentity
    user_text: str
    event_ids: tuple[str, ...]
    late_event_ids: tuple[str, ...] = ()
    last_event_at: AwareDatetime | None = None  # newest occurred_at among the turn's events
    first_received_at: AwareDatetime | None = None  # when the OLDEST event reached us (queue wait)
    inbound: tuple[InboundRef, ...] = ()  # channel evidence per event, in burst order
    resumed: bool = False  # True when an earlier owner had already opened this turn
    attempts: int = 0  # failed processing attempts so far (NOT waits for tool results)
    media: tuple[MediaReference, ...] = ()
    system_only: bool = False  # every event of the turn is runtime-originated (no contact input)


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
    # When the first send of this row was claimed (coordination time): the start of the window
    # in which a resend can still rely on the channel's idempotency memory.
    first_sent_at: AwareDatetime | None = None
    reconcile_attempts: int = 0
    channel_message_id: str | None = None  # the GATEWAY's id for this send (not the provider's)


class SendResult(_Frozen):
    """What a MessageSender reports. ACCEPTED means accepted for sending, not delivered/read."""

    status: OutboxStatus  # QUEUED | ACCEPTED | FAILED | UNKNOWN (enforced below)
    provider_message_id: str | None = None
    # When the CHANNEL accepted the message, in the channel's own clock (INV-026). `None` when the
    # channel gives no comparable timestamp: the runtime never substitutes its local time.
    provider_accepted_at: AwareDatetime | None = None
    retryable: bool = False
    channel_message_id: str | None = None  # the gateway's id, known as soon as it took the send

    @model_validator(mode="after")
    def _only_sender_outcomes(self) -> SendResult:
        allowed = {
            OutboxStatus.QUEUED,
            OutboxStatus.ACCEPTED,
            OutboxStatus.FAILED,
            OutboxStatus.UNKNOWN,
        }
        if self.status not in allowed:
            raise ValueError(f"a sender cannot report {self.status.value}")
        if self.provider_accepted_at is not None and self.status is not OutboxStatus.ACCEPTED:
            raise ValueError("provider_accepted_at only applies to an ACCEPTED message")
        return self


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


class RecoverySnapshot(_Frozen):
    """The recovery contract as it was when the operation was prepared (frozen)."""

    strategy: Literal["safe_retry", "retry_same_key", "status_lookup", "human_handoff"]
    lookup_capability: str | None = None
    # For status_lookup the lookup itself is frozen too: the concrete tool args, provider and
    # destination fingerprint. Reconciliation must ask the SAME system the write went to, never
    # whatever the lookup capability resolves to after a deploy (INV-027).
    lookup_intent: ExecutionIntent | None = None
    # How a lookup result becomes the ORIGINAL capability result (None: schemas are identical).
    result_map: MappingSpec | None = None
    absent_codes: tuple[str, ...] = ()
    idempotency_supported: bool = False


class ExecutionIntent(_Frozen):
    """The concrete external operation, frozen at PREPARE (INV-023).

    Retry and reconciliation execute *this*: the already-mapped tool arguments against the
    same provider/connection/endpoint. They never re-map arguments or re-resolve the binding
    with whatever definitions happen to be deployed later; if the resolved operation no longer
    matches `binding_fingerprint` they refuse and escalate.
    """

    capability: str
    tool_name: str
    provider: str
    connection: str | None = None
    tool_args: dict[str, Any]
    tool_args_hash: str
    binding_fingerprint: str
    connection_fingerprint: str | None = None  # resolved destination, no secrets (INV-027)
    recovery: RecoverySnapshot


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
    intent: ExecutionIntent
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
