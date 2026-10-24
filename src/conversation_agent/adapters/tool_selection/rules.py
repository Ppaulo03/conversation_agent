from __future__ import annotations

import re
from collections.abc import Iterable

from conversation_agent.core.definitions.tool_selection import ToolRule
from conversation_agent.core.models.llm import LLMToolDefinition
from conversation_agent.core.temporal_ptbr import fold
from conversation_agent.ports.tool_selector import ToolScore


class RulesToolSelector:
    """Keyword relevance per tool (free, deterministic). A keyword matches from the start of a word
    (so "horario" also finds "horarios"). The score depends on how many keywords matched, not on
    how many were declared, so a tool with a long list is not penalised; a tool without a rule
    scores 0."""

    def __init__(self, rules: Iterable[ToolRule]) -> None:
        self._rules: dict[str, list[re.Pattern[str]]] = {}
        for rule in rules:
            llm_name = rule.capability.replace(".", "__")
            patterns = [
                re.compile(rf"(?<![a-z0-9]){re.escape(folded)}")
                for kw in rule.keywords
                if (folded := fold(kw))
            ]
            self._rules[llm_name] = patterns

    async def rank(
        self, message: str, candidates: tuple[LLMToolDefinition, ...]
    ) -> list[ToolScore]:
        text = fold(message)
        scores: list[ToolScore] = []
        for tool in candidates:
            patterns = self._rules.get(tool.name, [])
            hits = sum(1 for p in patterns if p.search(text))
            scores.append(ToolScore(tool.name, 1 - 0.5**hits))  # 0 hits: 0; 1: 0.5; 2: 0.75
        return scores
