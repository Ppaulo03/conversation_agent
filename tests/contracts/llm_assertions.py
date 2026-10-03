"""Shape assertions every LLMProvider must satisfy (shared by stubbed and live runs)."""

from __future__ import annotations

from conversation_agent.core.models.llm import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMStopReason,
    LLMUsage,
    TextPart,
    ToolCallPart,
)


def assert_canonical_response(response: LLMResponse) -> None:
    assert isinstance(response, LLMResponse)
    assert isinstance(response.usage, LLMUsage)
    assert response.usage.input_tokens >= 0
    assert response.usage.output_tokens >= 0
    assert isinstance(response.stop_reason, LLMStopReason)
    assert all(isinstance(p, TextPart | ToolCallPart) for p in response.parts)


def assert_text_response(response: LLMResponse) -> None:
    assert_canonical_response(response)
    assert response.text.strip() != ""
    assert response.tool_calls == ()
    assert response.stop_reason is LLMStopReason.END_TURN


def assert_tool_call_response(response: LLMResponse, name: str) -> None:
    assert_canonical_response(response)
    assert response.stop_reason is LLMStopReason.TOOL_USE
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.id
    assert call.name == name
    assert isinstance(call.arguments, dict)


def simple_request(text: str = "Say OK.") -> LLMRequest:
    return LLMRequest(
        system="You are a test assistant.",
        messages=(LLMMessage.text("user", text),),
        max_tokens=64,
    )
