"""Anthropic adapter: canonical LLM types <-> Anthropic Messages API.

No Anthropic type leaves this module; SDK exceptions become `LLMProviderError`.
"""

from __future__ import annotations

from typing import Any, Protocol

import anthropic

from conversation_agent.core.errors import LLMProviderError
from conversation_agent.core.models.llm import (
    LLMContentPart,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMStopReason,
    LLMToolCall,
    LLMUsage,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)

_STOP_REASONS = {
    "end_turn": LLMStopReason.END_TURN,
    "stop_sequence": LLMStopReason.END_TURN,
    "tool_use": LLMStopReason.TOOL_USE,
    "max_tokens": LLMStopReason.MAX_TOKENS,
}


class AnthropicClient(Protocol):
    """The slice of `anthropic.AsyncAnthropic` this adapter uses (stubbable in tests):
    an object whose `.messages.create(**kwargs)` is awaitable."""

    @property
    def messages(self) -> Any: ...


def _part_to_block(part: LLMContentPart) -> dict[str, Any]:
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text}
    if isinstance(part, ToolCallPart):
        return {
            "type": "tool_use",
            "id": part.call.id,
            "name": part.call.name,
            "input": part.call.arguments,
        }
    assert isinstance(part, ToolResultPart)
    return {
        "type": "tool_result",
        "tool_use_id": part.tool_call_id,
        "content": part.content,
        "is_error": part.is_error,
    }


def _message_to_anthropic(message: LLMMessage) -> dict[str, Any]:
    return {"role": message.role, "content": [_part_to_block(p) for p in message.parts]}


class AnthropicLLM:
    def __init__(self, client: AnthropicClient, model: str) -> None:
        self._client = client
        self._model = model

    @classmethod
    def from_api_key(cls, api_key: str, model: str) -> AnthropicLLM:
        return cls(anthropic.AsyncAnthropic(api_key=api_key), model)

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)  # AsyncAnthropic.close(); stubs may lack it
        if close is not None:
            await close()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [_message_to_anthropic(m) for m in request.messages],
        }
        tools = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in request.tools
        ]
        so = request.structured_output
        if so is not None:
            # Structured output is implemented as a forced tool call.
            tools.append(
                {"name": so.name, "description": so.description, "input_schema": so.schema_}
            )
            kwargs["tool_choice"] = {"type": "tool", "name": so.name}
        if tools:
            kwargs["tools"] = tools
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature

        try:
            raw = await self._client.messages.create(**kwargs)
        except anthropic.APIStatusError as exc:
            raise LLMProviderError(
                f"Anthropic API error (HTTP {exc.status_code})",
                retryable=exc.status_code in (408, 409, 429) or exc.status_code >= 500,
            ) from exc
        except anthropic.APIConnectionError as exc:  # includes timeouts
            raise LLMProviderError("Anthropic connection error", retryable=True) from exc
        except anthropic.AnthropicError as exc:
            raise LLMProviderError("Anthropic SDK error") from exc

        return self._to_response(raw, structured_name=so.name if so else None)

    @staticmethod
    def _to_response(raw: Any, structured_name: str | None) -> LLMResponse:
        parts: list[LLMContentPart] = []
        structured: dict[str, Any] | None = None
        for block in raw.content:
            if block.type == "text":
                parts.append(TextPart(text=block.text))
            elif block.type == "tool_use":
                if structured_name is not None and block.name == structured_name:
                    structured = dict(block.input)
                else:
                    parts.append(
                        ToolCallPart(
                            call=LLMToolCall(
                                id=block.id, name=block.name, arguments=dict(block.input)
                            )
                        )
                    )
        stop = _STOP_REASONS.get(raw.stop_reason, LLMStopReason.OTHER)
        if structured is not None:
            stop = LLMStopReason.END_TURN
        return LLMResponse(
            parts=tuple(parts),
            stop_reason=stop,
            usage=LLMUsage(
                input_tokens=raw.usage.input_tokens or 0,
                output_tokens=raw.usage.output_tokens or 0,
                cache_read_tokens=getattr(raw.usage, "cache_read_input_tokens", None) or 0,
                cache_write_tokens=getattr(raw.usage, "cache_creation_input_tokens", None) or 0,
            ),  # Anthropic's `input_tokens` already excludes the cached ones
            structured=structured,
            provider="anthropic",
            model=getattr(raw, "model", None),
        )
