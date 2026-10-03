"""Turn Journal contract (DESIGN §20A). Step identity is (turn_id, step_index, step_type)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class JournalStepType(StrEnum):
    INBOUND_AGGREGATED = "INBOUND_AGGREGATED"
    OWNERSHIP_CHECK = "OWNERSHIP_CHECK"
    CONFIRMATION_DECISION = "CONFIRMATION_DECISION"
    ACTION_TRANSITION = "ACTION_TRANSITION"
    LLM_REQUEST = "LLM_REQUEST"
    LLM_RESPONSE = "LLM_RESPONSE"
    CAPABILITY_REQUEST = "CAPABILITY_REQUEST"
    TOOL_PREPARED = "TOOL_PREPARED"
    POLICY_DECISION = "POLICY_DECISION"
    TOOL_RESULT = "TOOL_RESULT"
    TURN_COMPLETED = "TURN_COMPLETED"


class JournalEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    turn_id: str
    step_index: int
    step_type: JournalStepType
    request_hash: str | None = None
    logical_step_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
