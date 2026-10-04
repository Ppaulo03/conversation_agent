"""Flow runtime state: conversational data only (INV-008), JSON-safe, versionable."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from conversation_agent.core.models.tooling import ToolStatus


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StepResult(_Frozen):
    """What an Invoke step got back, tied to the inputs it was run with: change an input and
    the result is no longer valid (it does not match any more), nothing is re-used blindly."""

    input_hash: str
    status: ToolStatus
    data: dict[str, Any] | None = None
    error_code: str | None = None


class FlowInstance(_Frozen):
    instance_id: str
    flow_name: str
    status: Literal["active", "suspended"] = "active"
    slots: dict[str, Any] = Field(default_factory=dict)  # validated, normalised values
    results: dict[str, StepResult] = Field(default_factory=dict)
    options: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)  # Choose id -> shown
    # Choose id -> real options the message had no room to show: a stated time/date can still
    # select one of them, a number or an ordinal never does (they refer to what was shown).
    unlisted: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    awaiting_slot: str | None = None  # the slot the last question asked for
    awaiting_choice: str | None = None  # the Choose step whose options are on screen
    proposal_hash: str | None = None  # hash of the inputs last proposed for confirmation
    last_question: str = ""  # replayed when the flow is resumed after a digression
    unclear_count: int = 0
    digressions: int = 0
