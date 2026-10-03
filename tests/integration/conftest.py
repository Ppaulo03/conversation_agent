from __future__ import annotations

import os
from pathlib import Path

import pytest

from conversation_agent.app.llm_factory import LLMConfig, LLMConfigError, build_llm
from conversation_agent.ports.llm import LLMProvider
from support.live_budget import DEFAULT_DAILY_LIMIT, BudgetedLLM, DailyBudget

ROOT = Path(__file__).resolve().parents[2]
CALLS_NEEDED = 8  # upper bound for the whole live suite; below this we skip instead of starting


def live_budget() -> DailyBudget:
    limit = int(os.environ.get("LIVE_LLM_DAILY_CALLS", DEFAULT_DAILY_LIMIT))
    return DailyBudget(ROOT / ".live_llm_usage.json", limit)


@pytest.fixture
def live_llm() -> LLMProvider:
    """The configured real provider, wrapped in the daily call budget."""
    try:
        config = LLMConfig.from_env(os.environ)
    except LLMConfigError:
        pytest.skip("no LLM provider configured in the environment")
    budget = live_budget()
    if budget.remaining() < CALLS_NEEDED:
        pytest.skip(
            f"live LLM budget low ({budget.remaining()} calls left today, need {CALLS_NEEDED}); "
            "raise LIVE_LLM_DAILY_CALLS or wait for tomorrow"
        )
    return BudgetedLLM(build_llm(config), budget)
