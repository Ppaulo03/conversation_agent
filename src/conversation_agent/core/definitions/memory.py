"""Per-agent settings of contact memory: a name and a few short facts the agent keeps about each
contact ACROSS conversations. Off unless the agent has the block: without it nothing is loaded,
written or shown to the model, and the digest of existing agents is unchanged."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

MAX_FACTS_CAP = 100
MAX_FACT_CHARS_CAP = 1000
MAX_NAME_CHARS_CAP = 200


@dataclass(frozen=True)
class MemoryLimits:
    """Enforced where the write happens, never only in the prompt."""

    max_facts: int = 20
    max_fact_chars: int = 200
    max_name_chars: int = 60

    def __post_init__(self) -> None:
        if not 1 <= self.max_facts <= MAX_FACTS_CAP:
            raise ValueError(f"max_facts must be between 1 and {MAX_FACTS_CAP}")
        if not 1 <= self.max_fact_chars <= MAX_FACT_CHARS_CAP:
            raise ValueError(f"max_fact_chars must be between 1 and {MAX_FACT_CHARS_CAP}")
        if not 1 <= self.max_name_chars <= MAX_NAME_CHARS_CAP:
            raise ValueError(f"max_name_chars must be between 1 and {MAX_NAME_CHARS_CAP}")


class MemorySettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_facts: int = Field(default=20, ge=1, le=MAX_FACTS_CAP)
    max_fact_chars: int = Field(default=200, ge=1, le=MAX_FACT_CHARS_CAP)
    max_name_chars: int = Field(default=60, ge=1, le=MAX_NAME_CHARS_CAP)

    def limits(self) -> MemoryLimits:
        return MemoryLimits(self.max_facts, self.max_fact_chars, self.max_name_chars)
