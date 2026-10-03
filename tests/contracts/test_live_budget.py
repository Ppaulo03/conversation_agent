"""The live-test budget guard itself (no network)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.core.errors import LLMProviderError
from conversation_agent.core.models.llm import LLMMessage, LLMRequest
from support.live_budget import BudgetedLLM, BudgetExceededError, DailyBudget

REQUEST = LLMRequest(system="s", messages=(LLMMessage.text("user", "x"),))


def test_budget_counts_down_and_blocks_at_the_limit(tmp_path: Path) -> None:
    budget = DailyBudget(tmp_path / "usage.json", limit=2)
    assert budget.remaining() == 2
    budget.consume()
    budget.consume()
    assert budget.remaining() == 0
    with pytest.raises(BudgetExceededError):
        budget.consume()


def test_budget_resets_on_a_new_day(tmp_path: Path) -> None:
    day = [date(2026, 10, 5)]
    budget = DailyBudget(tmp_path / "usage.json", limit=1, today=lambda: day[0])
    budget.consume()
    assert budget.remaining() == 0
    day[0] += timedelta(days=1)
    assert budget.remaining() == 1


def test_corrupt_state_file_does_not_crash_and_counts_as_fresh_day(tmp_path: Path) -> None:
    path = tmp_path / "usage.json"
    path.write_text("not json", encoding="utf-8")
    assert DailyBudget(path, limit=3).remaining() == 3


async def test_every_call_consumes_even_failures_and_429_exhausts_the_day(tmp_path: Path) -> None:
    budget = DailyBudget(tmp_path / "usage.json", limit=5)
    llm = BudgetedLLM(
        FakeLLM(
            [
                text_response("ok"),
                LLMProviderError("boom"),
                LLMProviderError("LLM API error (HTTP 429)"),
            ]
        ),
        budget,
    )
    await llm.complete(REQUEST)
    assert budget.remaining() == 4
    with pytest.raises(LLMProviderError):
        await llm.complete(REQUEST)
    assert budget.remaining() == 3  # failed call still consumed
    with pytest.raises(LLMProviderError):
        await llm.complete(REQUEST)
    assert budget.remaining() == 0  # rate limited -> stop for today
    with pytest.raises(BudgetExceededError):
        await llm.complete(REQUEST)
