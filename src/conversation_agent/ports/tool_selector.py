from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from conversation_agent.core.models.llm import LLMToolDefinition


@dataclass(frozen=True)
class ToolScore:
    name: str  # an LLM tool name taken from the candidates
    score: float  # 0..1, higher = more relevant


class ToolSelector(Protocol):
    """Ranks the tools the model should see for a message.

    Contract (enforced by the framework, not trusted): the framework only keeps names that are in
    `candidates`; anything else the selector returns is dropped. A selector can therefore never
    add or authorize a tool. It may raise: the framework then falls back to the full list.
    """

    async def rank(
        self, message: str, candidates: tuple[LLMToolDefinition, ...]
    ) -> list[ToolScore]: ...
