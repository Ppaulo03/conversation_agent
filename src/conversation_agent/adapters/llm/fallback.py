"""One alternate provider for a failed model call; never replays a turn or business tool."""

from __future__ import annotations

import logging

from conversation_agent.core.errors import LLMProviderError
from conversation_agent.core.models.llm import LLMRequest, LLMResponse
from conversation_agent.ports.llm import LLMProvider

log = logging.getLogger(__name__)


class FallbackLLM:
    def __init__(self, primary: LLMProvider, fallback: LLMProvider) -> None:
        self._primary = primary
        self._fallback = fallback

    async def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            return await self._primary.complete(request)
        except LLMProviderError as exc:
            log.warning("llm.fallback", extra={"fields": {"retryable": exc.retryable}})
        return await self._fallback.complete(request)

    async def aclose(self) -> None:
        try:
            close = getattr(self._primary, "aclose", None)
            if close is not None:
                await close()
        finally:
            close = getattr(self._fallback, "aclose", None)
            if close is not None:
                await close()
