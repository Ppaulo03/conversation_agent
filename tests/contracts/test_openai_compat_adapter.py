"""OpenAI-compatible adapter specifics: request translation, auth, error mapping."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from conversation_agent.adapters.llm.openai_compat import OpenAICompatLLM
from conversation_agent.core.errors import LLMProviderError
from conversation_agent.core.models.llm import (
    LLMMessage,
    LLMRequest,
    LLMStructuredOutput,
    LLMToolCall,
    LLMToolDefinition,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)

OK = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
}


def llm_with(handler: Any) -> OpenAICompatLLM:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatLLM(client, base_url="http://llm.test/v1/", api_key="sekret", model="m-1")


async def test_request_translation_and_auth_header() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=OK)

    request = LLMRequest(
        system="sys",
        messages=(
            LLMMessage.text("user", "hi"),
            LLMMessage(
                role="assistant",
                parts=(
                    TextPart(text="looking"),
                    ToolCallPart(call=LLMToolCall(id="c1", name="lookup", arguments={"a": 1})),
                ),
            ),
            LLMMessage(role="user", parts=(ToolResultPart(tool_call_id="c1", content="data"),)),
        ),
        tools=(LLMToolDefinition(name="lookup", description="d", input_schema={"type": "object"}),),
        max_tokens=50,
    )
    await llm_with(handler).complete(request)

    assert seen["url"] == "http://llm.test/v1/chat/completions"
    assert seen["auth"] == "Bearer sekret"
    body = seen["body"]
    assert body["model"] == "m-1" and body["max_tokens"] == 50
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "looking",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"a": 1}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "data"},
    ]
    assert body["tools"][0]["function"]["parameters"] == {"type": "object"}


async def test_structured_output_forces_a_function_call() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=OK)

    request = LLMRequest(
        system="s",
        messages=(LLMMessage.text("user", "x"),),
        structured_output=LLMStructuredOutput(name="result", schema={"type": "object"}),
    )
    await llm_with(handler).complete(request)
    assert seen["tool_choice"] == {"type": "function", "function": {"name": "result"}}


@pytest.mark.parametrize(("status", "retryable"), [(429, True), (503, True), (401, False)])
async def test_http_errors_map_retryability_and_never_leak_the_key(
    status: int, retryable: bool
) -> None:
    llm = llm_with(lambda r: httpx.Response(status, json={"error": {"message": "sekret"}}))
    request = LLMRequest(system="s", messages=(LLMMessage.text("user", "x"),))
    with pytest.raises(LLMProviderError) as info:
        await llm.complete(request)
    assert info.value.retryable is retryable
    assert "sekret" not in str(info.value)


BAD_ARGUMENTS = {
    "choices": [
        {
            "message": {
                "tool_calls": [{"id": "1", "function": {"name": "t", "arguments": "{not json"}}]
            },
            "finish_reason": "tool_calls",
        }
    ]
}


async def test_error_includes_provider_error_code_but_not_message() -> None:
    body = {"error": {"code": "model_not_found", "message": "secret detail"}}
    llm = llm_with(lambda r: httpx.Response(404, json=body))
    request = LLMRequest(system="s", messages=(LLMMessage.text("user", "x"),))
    with pytest.raises(LLMProviderError) as info:
        await llm.complete(request)
    assert "model_not_found" in str(info.value)
    assert "secret detail" not in str(info.value)


@pytest.mark.parametrize("body", [{}, {"choices": []}, BAD_ARGUMENTS])
async def test_malformed_provider_payload_is_a_provider_error(body: dict[str, Any]) -> None:
    llm = llm_with(lambda r: httpx.Response(200, json=body))
    request = LLMRequest(system="s", messages=(LLMMessage.text("user", "x"),))
    with pytest.raises(LLMProviderError):
        await llm.complete(request)
