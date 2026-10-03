"""Canonical, SDK-independent LLM contract (DESIGN §25.1).

Adapters translate these types to/from provider types. Nothing proprietary leaks here.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from conversation_agent.core.canonical import stable_hash


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LLMStopReason(StrEnum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    OTHER = "other"


class LLMUsage(_Frozen):
    input_tokens: int = 0
    output_tokens: int = 0


class LLMToolCall(_Frozen):
    """A tool call proposed by the model. `id` is ephemeral and never an identity (§9.1)."""

    id: str
    name: str
    arguments: dict[str, Any]


class LLMToolDefinition(_Frozen):
    name: str
    description: str
    input_schema: dict[str, Any]


class LLMStructuredOutput(_Frozen):
    """Request a JSON object conforming to `schema` (adapters pick the mechanism)."""

    name: str
    description: str = ""
    schema_: dict[str, Any] = Field(alias="schema")

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class TextPart(_Frozen):
    type: Literal["text"] = "text"
    text: str


class ToolCallPart(_Frozen):
    type: Literal["tool_call"] = "tool_call"
    call: LLMToolCall


class ToolResultPart(_Frozen):
    """Result of a tool call, always *data* (INV-013), never instructions."""

    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    content: str
    is_error: bool = False


type LLMContentPart = Annotated[
    TextPart | ToolCallPart | ToolResultPart, Field(discriminator="type")
]


class LLMMessage(_Frozen):
    role: Literal["user", "assistant"]
    parts: tuple[LLMContentPart, ...]

    @classmethod
    def text(cls, role: Literal["user", "assistant"], text: str) -> LLMMessage:
        return cls(role=role, parts=(TextPart(text=text),))


class LLMRequest(_Frozen):
    system: str
    messages: tuple[LLMMessage, ...]
    tools: tuple[LLMToolDefinition, ...] = ()
    structured_output: LLMStructuredOutput | None = None
    max_tokens: int = 1024
    temperature: float | None = None
    # Stable id (turn_id + step_index + request_hash); excluded from request_hash.
    request_id: str | None = None


def llm_request_hash(request: LLMRequest) -> str:
    """Identity of a request's *content* (the delivery `request_id` is excluded)."""
    return stable_hash(request.model_dump(mode="json", by_alias=True, exclude={"request_id"}))


class LLMResponse(_Frozen):
    parts: tuple[LLMContentPart, ...]
    stop_reason: LLMStopReason
    usage: LLMUsage = LLMUsage()
    structured: dict[str, Any] | None = None

    @property
    def text(self) -> str:
        return "".join(p.text for p in self.parts if isinstance(p, TextPart))

    @property
    def tool_calls(self) -> tuple[LLMToolCall, ...]:
        return tuple(p.call for p in self.parts if isinstance(p, ToolCallPart))
