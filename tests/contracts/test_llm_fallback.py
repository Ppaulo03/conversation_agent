"""Fallback stays inside one canonical model call, including tool and structured requests."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

import anthropic
import httpx
import pytest

from conversation_agent.adapters.llm.anthropic import AnthropicLLM
from conversation_agent.adapters.llm.fake import FakeLLM, text_response, tool_call_response
from conversation_agent.adapters.llm.fallback import FallbackLLM
from conversation_agent.adapters.llm.openai_compat import OpenAICompatLLM
from conversation_agent.app.llm_factory import LLMConfig, build_llm, close_llm
from conversation_agent.core.errors import LLMBudgetDeferredError, LLMProviderError
from conversation_agent.core.models.llm import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMStopReason,
    LLMStructuredOutput,
    LLMToolCall,
    LLMToolDefinition,
    LLMUsage,
    ToolCallPart,
    ToolResultPart,
)


@pytest.mark.parametrize("retryable", [False, True])
@pytest.mark.parametrize("structured", [False, True])
async def test_fallback_receives_the_whole_request_and_returns_its_metadata(
    retryable: bool, structured: bool, caplog: pytest.LogCaptureFixture
) -> None:
    request = LLMRequest(
        system="private system prompt",
        messages=(
            LLMMessage.text("user", "private question"),
            LLMMessage(
                role="assistant",
                parts=(ToolCallPart(call=LLMToolCall(id="c1", name="lookup", arguments={})),),
            ),
            LLMMessage(
                role="user", parts=(ToolResultPart(tool_call_id="c1", content='{"value":7}'),)
            ),
        ),
        tools=(
            LLMToolDefinition(name="lookup", description="lookup", input_schema={"type": "object"}),
        ),
        structured_output=LLMStructuredOutput(name="result", schema={"type": "object"})
        if structured
        else None,
        max_tokens=123,
        temperature=0.2,
        request_id="stable-id",
    )
    answer = LLMResponse(
        parts=tool_call_response("lookup", {}).parts if not structured else (),
        stop_reason=LLMStopReason.END_TURN if structured else LLMStopReason.TOOL_USE,
        structured={"ok": True} if structured else None,
        usage=LLMUsage(input_tokens=11, output_tokens=7),
        provider="alternate",
        model="alternate-model",
    )
    primary = FakeLLM([LLMProviderError("secret exception", retryable=retryable)])
    fallback = FakeLLM([answer])
    with caplog.at_level(logging.WARNING):
        result = await FallbackLLM(primary, fallback).complete(request)
    assert result is answer
    assert primary.requests == fallback.requests == [request]
    assert fallback.requests[0] is request
    assert "llm.fallback" in caplog.text
    assert all(secret not in caplog.text for secret in ("private", "secret exception", "value"))


async def test_success_never_uses_fallback_and_next_call_retries_primary() -> None:
    request = LLMRequest(system="", messages=(LLMMessage.text("user", "hi"),))
    primary = FakeLLM([LLMProviderError("down"), text_response("recovered")])
    fallback = FakeLLM([text_response("backup")])
    llm = FallbackLLM(primary, fallback)
    assert (await llm.complete(request)).text == "backup"
    assert (await llm.complete(request)).text == "recovered"
    assert len(primary.requests) == 2 and len(fallback.requests) == 1


async def test_both_fail_once_and_the_last_error_propagates() -> None:
    primary = FakeLLM([LLMProviderError("primary")])
    error = LLMProviderError("backup")
    fallback = FakeLLM([error])
    with pytest.raises(LLMProviderError) as caught:
        await FallbackLLM(primary, fallback).complete(LLMRequest(system="", messages=()))
    assert caught.value is error
    assert len(primary.requests) == len(fallback.requests) == 1


@pytest.mark.parametrize("error", [ValueError("bug"), LLMBudgetDeferredError(datetime.now(UTC))])
async def test_non_provider_errors_do_not_use_fallback(error: Exception) -> None:
    def fail(request: LLMRequest) -> LLMResponse:
        raise error

    fallback = FakeLLM([text_response("must not run")])
    with pytest.raises(type(error)):
        await FallbackLLM(FakeLLM([fail]), fallback).complete(LLMRequest(system="", messages=()))
    assert fallback.requests == []


async def test_cancellation_does_not_use_fallback() -> None:
    class Cancelled:
        async def complete(self, request: LLMRequest) -> LLMResponse:
            raise asyncio.CancelledError

    fallback = FakeLLM([text_response("must not run")])
    with pytest.raises(asyncio.CancelledError):
        await FallbackLLM(Cancelled(), fallback).complete(LLMRequest(system="", messages=()))
    assert fallback.requests == []


@pytest.mark.parametrize("close_fails", [False, True])
async def test_both_clients_are_closed_even_if_primary_close_fails(close_fails: bool) -> None:
    class Closable(FakeLLM):
        closed = False

        async def aclose(self) -> None:
            self.closed = True
            if close_fails and self is primary:
                raise RuntimeError("close failed")

    primary, fallback = Closable([]), Closable([])
    llm = FallbackLLM(primary, fallback)
    if close_fails:
        with pytest.raises(RuntimeError):
            await close_llm(llm)
    else:
        await close_llm(llm)
    assert primary.closed and fallback.closed


async def test_factory_supports_separate_provider_configs() -> None:
    llm = build_llm(
        LLMConfig("anthropic", "primary-key", "primary-model"),
        fallback=LLMConfig("groq", "backup-key", "backup-model"),
    )
    try:
        assert isinstance(llm, FallbackLLM)
    finally:
        await close_llm(llm)


@pytest.mark.parametrize("status", [400, 401, 429, 503, None])
async def test_native_anthropic_errors_fall_back_to_openai_compatible_with_tool_history(
    status: int | None,
) -> None:
    class Messages:
        async def create(self, **kwargs: Any) -> Any:
            request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            if status is None:
                raise anthropic.APITimeoutError(request=request)
            raise anthropic.APIStatusError(
                "failed", response=httpx.Response(status, request=request), body=None
            )

    class Client:
        messages = Messages()

    sent: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer backup-key"
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(
            200,
            json={
                "model": "backup-model",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "result1",
                                    "function": {"name": "result", "arguments": '{"ok":true}'},
                                }
                            ]
                        },
                    }
                ],
            },
        )

    fallback = OpenAICompatLLM(
        httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        base_url="https://alternate.test/v1",
        api_key="backup-key",
        model="backup-model",
    )
    llm = FallbackLLM(AnthropicLLM(Client(), "primary-model"), fallback)
    request = LLMRequest(
        system="system",
        messages=(
            LLMMessage.text("user", "question"),
            LLMMessage(
                role="assistant",
                parts=(ToolCallPart(call=LLMToolCall(id="toolu_1", name="lookup", arguments={})),),
            ),
            LLMMessage(
                role="user", parts=(ToolResultPart(tool_call_id="toolu_1", content='{"value":7}'),)
            ),
        ),
        tools=(
            LLMToolDefinition(name="lookup", description="lookup", input_schema={"type": "object"}),
        ),
        structured_output=LLMStructuredOutput(name="result", schema={"type": "object"}),
    )
    try:
        result = await llm.complete(request)
        assert result.structured == {"ok": True}
        assert result.model == "backup-model"
        assert len(sent) == 1
        assert sent[0]["messages"][-1] == {
            "role": "tool",
            "tool_call_id": "toolu_1",
            "content": '{"value":7}',
        }
        assert sent[0]["tool_choice"]["function"]["name"] == "result"
        assert [tool["function"]["name"] for tool in sent[0]["tools"]] == ["lookup", "result"]
    finally:
        await close_llm(llm)
