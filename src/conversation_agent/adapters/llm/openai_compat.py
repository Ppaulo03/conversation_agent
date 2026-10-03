"""OpenAI-compatible chat-completions adapter (Groq, OpenAI, vLLM, ...), over httpx.

Canonical LLM types <-> `/chat/completions`. No provider JSON leaves this module and every
failure becomes `LLMProviderError`. The API key is only ever placed in the Authorization
header and never appears in errors.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

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

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_DEFAULT_MODEL = "llama-3.3-70b-versatile"

_FINISH_REASONS = {
    "stop": LLMStopReason.END_TURN,
    "tool_calls": LLMStopReason.TOOL_USE,
    "function_call": LLMStopReason.TOOL_USE,
    "length": LLMStopReason.MAX_TOKENS,
}


def _message_to_openai(message: LLMMessage) -> list[dict[str, Any]]:
    """One canonical message may become several (tool results are separate `tool` messages)."""
    out: list[dict[str, Any]] = []
    text = "".join(p.text for p in message.parts if isinstance(p, TextPart))
    calls = [p.call for p in message.parts if isinstance(p, ToolCallPart)]
    results = [p for p in message.parts if isinstance(p, ToolResultPart)]
    for result in results:
        out.append({"role": "tool", "tool_call_id": result.tool_call_id, "content": result.content})
    if message.role == "assistant":
        entry: dict[str, Any] = {"role": "assistant", "content": text or None}
        if calls:
            entry["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                }
                for c in calls
            ]
        out.append(entry)
    elif text:
        out.append({"role": "user", "content": text})
    return out


class OpenAICompatLLM:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._client = client
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds

    @classmethod
    def groq(cls, api_key: str, model: str = GROQ_DEFAULT_MODEL) -> OpenAICompatLLM:
        return cls(httpx.AsyncClient(), base_url=GROQ_BASE_URL, api_key=api_key, model=model)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        messages: list[dict[str, Any]] = [{"role": "system", "content": request.system}]
        for message in request.messages:
            messages.extend(_message_to_openai(message))
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": request.max_tokens,
        }
        tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in request.tools
        ]
        so = request.structured_output
        if so is not None:
            # Structured output as a forced function call (portable across compatible servers).
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": so.name,
                        "description": so.description,
                        "parameters": so.schema_,
                    },
                }
            )
            body["tool_choice"] = {"type": "function", "function": {"name": so.name}}
        if tools:
            body["tools"] = tools
        if request.temperature is not None:
            body["temperature"] = request.temperature

        try:
            response = await self._client.post(
                self._url,
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
            )
        except httpx.RequestError as exc:
            raise LLMProviderError(
                f"LLM connection error ({type(exc).__name__})", retryable=True
            ) from exc
        if response.status_code >= 400:
            code = response.status_code
            raise LLMProviderError(
                f"LLM API error (HTTP {code})",
                retryable=code in (408, 409, 429) or code >= 500,
            )
        try:
            return self._to_response(response.json(), so.name if so else None)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError("LLM returned an unusable response") from exc

    @staticmethod
    def _to_response(payload: dict[str, Any], structured_name: str | None) -> LLMResponse:
        choice = payload["choices"][0]
        message = choice["message"]
        parts: list[LLMContentPart] = []
        if message.get("content"):
            parts.append(TextPart(text=message["content"]))
        structured: dict[str, Any] | None = None
        for call in message.get("tool_calls") or []:
            fn = call["function"]
            arguments = json.loads(fn["arguments"] or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")
            if structured_name is not None and fn["name"] == structured_name:
                structured = arguments
            else:
                parts.append(
                    ToolCallPart(
                        call=LLMToolCall(id=call["id"], name=fn["name"], arguments=arguments)
                    )
                )
        stop = _FINISH_REASONS.get(choice.get("finish_reason"), LLMStopReason.OTHER)
        if structured is not None:
            stop = LLMStopReason.END_TURN
        usage = payload.get("usage") or {}
        return LLMResponse(
            parts=tuple(parts),
            stop_reason=stop,
            usage=LLMUsage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
            ),
            structured=structured,
        )
