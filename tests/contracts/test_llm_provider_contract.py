"""LLMProvider contract: FakeLLM and AnthropicLLM (stubbed SDK client) pass the same suite.

The live provider runs the shared shape assertions in tests/integration (explicit profile).
"""

from __future__ import annotations

from typing import Any, Protocol

import anthropic
import httpx
import pytest
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

from conversation_agent.adapters.llm.anthropic import AnthropicLLM
from conversation_agent.adapters.llm.fake import FakeLLM, text_response, tool_call_response
from conversation_agent.core.errors import LLMProviderError
from conversation_agent.core.models.llm import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMStopReason,
    LLMStructuredOutput,
    LLMToolCall,
    LLMToolDefinition,
    LLMUsage,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from conversation_agent.ports.llm import LLMProvider

from .llm_assertions import (
    assert_canonical_response,
    assert_text_response,
    assert_tool_call_response,
    simple_request,
)

TOOL = LLMToolDefinition(
    name="lookup", description="Looks something up.", input_schema={"type": "object"}
)


class Harness(Protocol):
    def replying_text(self, text: str) -> LLMProvider: ...
    def calling_tool(self, name: str, arguments: dict[str, Any]) -> LLMProvider: ...
    def replying_structured(self, data: dict[str, Any]) -> LLMProvider: ...
    def failing(self) -> LLMProvider: ...


class FakeHarness:
    def replying_text(self, text: str) -> LLMProvider:
        return FakeLLM([text_response(text)])

    def calling_tool(self, name: str, arguments: dict[str, Any]) -> LLMProvider:
        return FakeLLM([tool_call_response(name, arguments)])

    def replying_structured(self, data: dict[str, Any]) -> LLMProvider:
        return FakeLLM(
            [
                LLMResponse(
                    parts=(),
                    stop_reason=LLMStopReason.END_TURN,
                    usage=LLMUsage(input_tokens=1, output_tokens=1),
                    structured=data,
                )
            ]
        )

    def failing(self) -> LLMProvider:
        return FakeLLM([LLMProviderError("boom")])


class StubMessages:
    def __init__(self, result: Message | Exception) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Message:
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class StubClient:
    def __init__(self, result: Message | Exception) -> None:
        self.messages = StubMessages(result)


def sdk_message(content: list[Any], stop_reason: str) -> Message:
    return Message(
        id="msg_1",
        type="message",
        role="assistant",
        model="stub-model",
        content=content,
        stop_reason=stop_reason,  # type: ignore[arg-type]
        stop_sequence=None,
        usage=Usage(input_tokens=7, output_tokens=3),
    )


class AnthropicHarness:
    def _llm(self, result: Message | Exception) -> AnthropicLLM:
        return AnthropicLLM(StubClient(result), model="stub-model")

    def replying_text(self, text: str) -> LLMProvider:
        return self._llm(sdk_message([TextBlock(type="text", text=text)], "end_turn"))

    def calling_tool(self, name: str, arguments: dict[str, Any]) -> LLMProvider:
        block = ToolUseBlock(type="tool_use", id="toolu_1", name=name, input=arguments)
        return self._llm(sdk_message([block], "tool_use"))

    def replying_structured(self, data: dict[str, Any]) -> LLMProvider:
        block = ToolUseBlock(type="tool_use", id="toolu_2", name="result", input=data)
        return self._llm(sdk_message([block], "tool_use"))

    def failing(self) -> LLMProvider:
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        return self._llm(anthropic.APIConnectionError(request=request))


@pytest.fixture(params=[FakeHarness, AnthropicHarness], ids=["fake", "anthropic-stub"])
def harness(request: pytest.FixtureRequest) -> Harness:
    return request.param()  # type: ignore[no-any-return]


async def test_text_reply(harness: Harness) -> None:
    response = await harness.replying_text("OK, done.").complete(simple_request())
    assert_text_response(response)
    assert response.text == "OK, done."


async def test_tool_call(harness: Harness) -> None:
    provider = harness.calling_tool("lookup", {"q": "x"})
    request = simple_request().model_copy(update={"tools": (TOOL,)})
    response = await provider.complete(request)
    assert_tool_call_response(response, "lookup")
    assert response.tool_calls[0].arguments == {"q": "x"}


async def test_structured_output(harness: Harness) -> None:
    request = simple_request().model_copy(
        update={
            "structured_output": LLMStructuredOutput(
                name="result", schema={"type": "object", "properties": {"ok": {"type": "boolean"}}}
            )
        }
    )
    response = await harness.replying_structured({"ok": True}).complete(request)
    assert_canonical_response(response)
    assert response.structured == {"ok": True}


async def test_accepts_tool_round_trip_history(harness: Harness) -> None:
    history = LLMRequest(
        system="s",
        messages=(
            LLMMessage.text("user", "find x"),
            LLMMessage(
                role="assistant",
                parts=(
                    TextPart(text="Looking."),
                    ToolCallPart(call=LLMToolCall(id="c1", name="lookup", arguments={"q": "x"})),
                ),
            ),
            LLMMessage(
                role="user",
                parts=(ToolResultPart(tool_call_id="c1", content='{"status":"success"}'),),
            ),
        ),
        tools=(TOOL,),
    )
    assert_text_response(await harness.replying_text("Found it.").complete(history))


async def test_failures_are_provider_errors_not_sdk_exceptions(harness: Harness) -> None:
    with pytest.raises(LLMProviderError):
        await harness.failing().complete(simple_request())


# --- Anthropic-adapter specifics (translation correctness) ------------------------------


async def test_anthropic_request_translation() -> None:
    client = StubClient(sdk_message([TextBlock(type="text", text="ok")], "end_turn"))
    llm = AnthropicLLM(client, model="m-1")
    request = LLMRequest(
        system="sys",
        messages=(
            LLMMessage.text("user", "hi"),
            LLMMessage(
                role="assistant",
                parts=(ToolCallPart(call=LLMToolCall(id="c1", name="lookup", arguments={"a": 1})),),
            ),
            LLMMessage(
                role="user",
                parts=(ToolResultPart(tool_call_id="c1", content="data", is_error=True),),
            ),
        ),
        tools=(TOOL,),
        max_tokens=99,
    )
    await llm.complete(request)
    sent = client.messages.calls[0]
    assert sent["model"] == "m-1"
    assert sent["system"] == "sys"
    assert sent["max_tokens"] == 99
    assert sent["tools"] == [
        {"name": "lookup", "description": "Looks something up.", "input_schema": {"type": "object"}}
    ]
    assert sent["messages"][1]["content"] == [
        {"type": "tool_use", "id": "c1", "name": "lookup", "input": {"a": 1}}
    ]
    assert sent["messages"][2]["content"] == [
        {"type": "tool_result", "tool_use_id": "c1", "content": "data", "is_error": True}
    ]


async def test_anthropic_structured_output_forces_tool_choice() -> None:
    block = ToolUseBlock(type="tool_use", id="t", name="result", input={"ok": True})
    client = StubClient(sdk_message([block], "tool_use"))
    request = simple_request().model_copy(
        update={"structured_output": LLMStructuredOutput(name="result", schema={"type": "object"})}
    )
    response = await AnthropicLLM(client, model="m").complete(request)
    assert client.messages.calls[0]["tool_choice"] == {"type": "tool", "name": "result"}
    assert response.structured == {"ok": True}
    assert response.tool_calls == ()


@pytest.mark.parametrize(("status", "retryable"), [(429, True), (500, True), (400, False)])
async def test_anthropic_status_errors_map_retryability(status: int, retryable: bool) -> None:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.APIStatusError("x", response=httpx.Response(status, request=req), body=None)
    llm = AnthropicLLM(StubClient(err), model="m")
    with pytest.raises(LLMProviderError) as info:
        await llm.complete(simple_request())
    assert info.value.retryable is retryable
