from __future__ import annotations

from typing import Protocol

from conversation_agent.core.models.llm import LLMRequest, LLMResponse


class LLMProvider(Protocol):
    """Contract: returns canonical types only; failures are raised as `LLMProviderError`."""

    async def complete(self, request: LLMRequest) -> LLMResponse: ...
