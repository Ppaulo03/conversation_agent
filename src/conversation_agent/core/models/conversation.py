"""Conversational state only. Business state belongs to external systems (INV-008)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from conversation_agent.core.models.flow import FlowInstance
from conversation_agent.core.models.media import MediaArg
from conversation_agent.core.models.tooling import CapabilityRequest, ProposedAction


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ConversationIdentity(_Frozen):
    """Trusted identity, supplied by the inbound adapter. Never inferred by the LLM."""

    tenant_id: str
    channel_id: str
    conversation_id: str
    session_id: str
    contact_id: str


class ConversationMessage(_Frozen):
    role: Literal["user", "assistant"]
    text: str


class ConversationState(_Frozen):
    """What the framework owns for a conversation in the vertical slice.

    `proposals` holds at most one validated draft `CapabilityRequest` per protected
    capability (e.g. a "create" capability). A proposal is *not* a PendingAction: nothing
    is confirmed or executed until the Phase 3 protected-action machinery exists.
    """

    history: tuple[ConversationMessage, ...] = ()
    proposals: dict[str, CapabilityRequest] = Field(default_factory=dict)
    # Flow stack: the LAST item is the active flow, earlier ones are suspended (DESIGN §23.3).
    flows: tuple[FlowInstance, ...] = ()
    # Files the contact sent, addressable by handle (`media_1`...): references only, the newest
    # MAX_MEDIA_HANDLES. They live and die with the conversation (retention, erasure).
    media: tuple[MediaArg, ...] = ()
    media_seq: int = 0  # the last handle number given: handles are never reused

    @property
    def active_flow(self) -> FlowInstance | None:
        return self.flows[-1] if self.flows else None


class TurnOutcome(_Frozen):
    turn_id: str
    reply: str
    state: ConversationState
    llm_calls: int
    halted: Literal["step_limit", "llm_truncated", "token_budget"] | None = None
    # Protected actions proposed this turn (-> PendingAction + prompt) / re-asked this turn.
    proposed: tuple[ProposedAction, ...] = ()
    reprompt_action_id: str | None = None
    # A Flow handed the conversation to a person: ownership moves to HANDOFF_PENDING in the SAME
    # transaction that persists this reply.
    handoff_requested: bool = False
