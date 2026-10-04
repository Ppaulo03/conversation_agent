from __future__ import annotations

from typing import Protocol

from conversation_agent.core.llm_prices import PriceTable
from conversation_agent.core.models.llm_usage import LLMCallRecord, UsageQuery, UsageRow


class LLMUsageStore(Protocol):
    """The books of LLM spend. Append-only: a call is recorded once and never edited."""

    async def record(self, call: LLMCallRecord) -> None: ...

    async def report(self, query: UsageQuery, prices: PriceTable) -> list[UsageRow]:
        """Usage grouped as asked, priced at the date of each call. A model with no price in
        `prices` counts as `unpriced_calls` (and adds no cost), never as free."""
        ...
