"""Protected-action models (DESIGN §10). Confirmation is action-scoped and auditable."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from conversation_agent.core.models.runtime import OutboxStatus
from conversation_agent.core.models.tooling import CapabilityRequest


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PendingActionStatus(StrEnum):
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


TERMINAL_ACTION_STATUSES = frozenset(
    {
        PendingActionStatus.CONFIRMED,
        PendingActionStatus.REJECTED,
        PendingActionStatus.INVALIDATED,
        PendingActionStatus.EXPIRED,
    }
)


class PendingAction(_Frozen):
    """A protected capability call awaiting the user's explicit confirmation.

    `action_id` is created once and stays stable until a terminal state (INV-004). Every
    argument is protected by default: `args_hash` covers the whole canonical request.
    """

    tenant_id: str
    conversation_id: str
    action_id: str
    capability: str
    tool_name: str
    request: CapabilityRequest
    args_hash: str
    protected_fields: tuple[str, ...]
    summary: str
    created_from_turn: str
    latest_prompt_outbox_id: str | None = None
    confirmation_attempts: int = 0
    expires_at: AwareDatetime | None = None
    status: PendingActionStatus = PendingActionStatus.PENDING_CONFIRMATION


ConfirmationDecision = Literal["confirm", "reject", "modify", "unclear"]


class ActionConfirmation(_Frozen):
    action_id: str
    inbound_message_id: str
    prompt_outbox_id: str
    reply_to_provider_message_id: str | None = None
    decision: ConfirmationDecision
    interpreter: Literal["rule", "llm", "budget_deferred"]
    confidence: float | None = None
    occurred_at: AwareDatetime
    confirmed_at: AwareDatetime | None = None


class PromptRecord(_Frozen):
    """What eligibility needs to know about one confirmation prompt (an outbox row)."""

    outbox_id: str
    status: OutboxStatus
    provider_message_id: str | None = None
    provider_accepted_at: datetime | None = None


class PromptEligibility(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    PROMPT_NOT_ACCEPTED = "PROMPT_NOT_ACCEPTED"  # QUEUED/SENDING/UNKNOWN/FAILED... (INV-022)
    BEFORE_PROMPT = "BEFORE_PROMPT"  # the answer predates the prompt: not a reply to it
    AMBIGUOUS = "AMBIGUOUS"  # clock zone or no comparable evidence: ask again
    UNRELATED_REPLY = "UNRELATED_REPLY"  # explicit reply-to something else
