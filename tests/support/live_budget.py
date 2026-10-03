"""Daily call budget for live LLM tests (providers like Groq have low daily limits).

State lives in a git-ignored JSON file at the repo root: {"date": "YYYY-MM-DD", "used": N}.
Every call to the real provider consumes one unit, failed calls included. A 429 from the
provider exhausts the day's budget so later runs do not keep hammering the API.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from pathlib import Path

from conversation_agent.core.errors import LLMProviderError
from conversation_agent.core.models.llm import LLMRequest, LLMResponse
from conversation_agent.ports.llm import LLMProvider

DEFAULT_DAILY_LIMIT = 20


class BudgetExceededError(RuntimeError):
    pass


class DailyBudget:
    def __init__(self, path: Path, limit: int, today: Callable[[], date] = date.today) -> None:
        self._path = path
        self._limit = limit
        self._today = today

    def _used(self) -> int:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if data.get("date") == self._today().isoformat():
                return int(data["used"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return 0

    def _write(self, used: int) -> None:
        payload = {"date": self._today().isoformat(), "used": used}
        self._path.write_text(json.dumps(payload), encoding="utf-8")

    def remaining(self) -> int:
        return max(self._limit - self._used(), 0)

    def consume(self) -> None:
        used = self._used()
        if used >= self._limit:
            raise BudgetExceededError(f"live LLM budget exhausted ({self._limit}/day)")
        self._write(used + 1)

    def exhaust(self) -> None:
        self._write(self._limit)


class BudgetedLLM:
    def __init__(self, inner: LLMProvider, budget: DailyBudget) -> None:
        self._inner = inner
        self._budget = budget

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self._budget.consume()
        try:
            return await self._inner.complete(request)
        except LLMProviderError as exc:
            if "HTTP 429" in str(exc):
                self._budget.exhaust()
            raise
