"""Optional gate checked immediately before an LLM request is journaled and sent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class LLMCallAdmission:
    """`retry_at` is set only when the call must wait for a budget period reset."""

    retry_at: datetime | None = None

    @property
    def admitted(self) -> bool:
        return self.retry_at is None


class LLMCallGate(Protocol):
    async def admit_llm_call(self, tenant_id: str) -> LLMCallAdmission: ...
