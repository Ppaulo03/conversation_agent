from __future__ import annotations

from typing import Protocol

from conversation_agent.core.llm_budget import BudgetStatus


class BudgetGate(Protocol):
    async def status(self, tenant_id: str) -> BudgetStatus | None:
        """Where the tenant stands against its LLM budget; None when it has none."""
        ...
