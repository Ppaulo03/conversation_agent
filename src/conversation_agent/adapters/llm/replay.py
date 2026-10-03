"""ReplayLLM: serves recorded responses by request content hash; fails on a miss.

Cassettes can be derived from a Turn Journal (the journal stays the authoritative record).
"""

from __future__ import annotations

from conversation_agent.core.errors import LLMProviderError
from conversation_agent.core.models.journal import JournalEntry, JournalStepType
from conversation_agent.core.models.llm import LLMRequest, LLMResponse, llm_request_hash


class ReplayLLM:
    def __init__(self, cassette: dict[str, LLMResponse]) -> None:
        self._cassette = dict(cassette)
        self.calls = 0

    @classmethod
    def from_journal(cls, entries: list[JournalEntry]) -> ReplayLLM:
        cassette = {
            e.request_hash: LLMResponse.model_validate(e.payload)
            for e in entries
            if e.step_type is JournalStepType.LLM_RESPONSE and e.request_hash is not None
        }
        return cls(cassette)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        response = self._cassette.get(llm_request_hash(request))
        if response is None:
            raise LLMProviderError("ReplayLLM: no recorded response for this request")
        return response
