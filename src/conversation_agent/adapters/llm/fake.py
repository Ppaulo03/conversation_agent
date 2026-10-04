"""Scripted LLM for deterministic tests and evals (never used against a real provider)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from conversation_agent.core.errors import LLMProviderError
from conversation_agent.core.models.llm import (
    LLMRequest,
    LLMResponse,
    LLMStopReason,
    LLMToolCall,
    LLMUsage,
    TextPart,
    ToolCallPart,
)

Script = LLMResponse | LLMProviderError | Callable[[LLMRequest], LLMResponse]


def text_response(text: str) -> LLMResponse:
    return LLMResponse(
        parts=(TextPart(text=text),),
        stop_reason=LLMStopReason.END_TURN,
        usage=LLMUsage(input_tokens=1, output_tokens=1),
    )


def tool_call_response(
    name: str, arguments: dict[str, Any], *, call_id: str = "call_1", text: str = ""
) -> LLMResponse:
    parts: list[TextPart | ToolCallPart] = [TextPart(text=text)] if text else []
    parts.append(ToolCallPart(call=LLMToolCall(id=call_id, name=name, arguments=arguments)))
    return LLMResponse(
        parts=tuple(parts),
        stop_reason=LLMStopReason.TOOL_USE,
        usage=LLMUsage(input_tokens=1, output_tokens=1),
    )


class FakeLLM:
    """Pops one scripted item per call. A callable item may inspect the request
    (e.g. to react to what the engine put in the tool results)."""

    def __init__(self, script: list[Script]) -> None:
        self._script = list(script)
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self._script:
            raise LLMProviderError("FakeLLM script exhausted")
        item = self._script.pop(0)
        if isinstance(item, LLMProviderError):
            raise item
        if isinstance(item, LLMResponse):
            return item
        return item(request)

    @property
    def calls(self) -> int:
        return len(self.requests)


def structured_response(data: dict[str, Any]) -> LLMResponse:
    """A model answer that is structured output (flow understanding, confirmation decisions)."""
    return LLMResponse(
        parts=(),
        stop_reason=LLMStopReason.END_TURN,
        usage=LLMUsage(input_tokens=1, output_tokens=1),
        structured=data,
    )
