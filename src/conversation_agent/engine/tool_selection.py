"""Tool selection stage: decides which allowed tools the model sees (ToolSelectionSettings).

The result is ALWAYS a subset of the allowed catalog (INV: selected ⊆ allowed). The decision is a
journal step, so a replay shows the model exactly the list the first run did.
"""

from __future__ import annotations

import contextlib
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol

from conversation_agent.core.definitions.tool_selection import ToolSelectionSettings
from conversation_agent.core.models.llm import LLMToolDefinition
from conversation_agent.ports.tool_selector import ToolSelector

WINDOW = 50  # recent triggered selections the fallback rate is measured over
MIN_SAMPLES = 20  # no auto-disable before this many


class ToolSelectionMetrics(Protocol):
    def record_tool_selection(self, *, agent_id: str, agent_version: str, outcome: str) -> None:
        """`outcome`: applied | skipped | provider_error | fallback | auto_disabled."""
        ...


def estimate_tokens(tool: LLMToolDefinition) -> int:
    """Cheap, provider-independent estimate (~4 chars/token of the serialized definition)."""
    raw = json.dumps(
        [tool.name, tool.description, tool.input_schema], ensure_ascii=False, sort_keys=True
    )
    return (len(raw) + 3) // 4


@dataclass
class SelectionMonitor:
    """Fallback accounting shared by the engines of one runtime (an engine lives for one turn)."""

    agent_id: str
    agent_version: str
    metrics: ToolSelectionMetrics | None = None
    _recent: deque[bool] = field(default_factory=lambda: deque(maxlen=WINDOW))
    disabled: bool = False

    def record(self, outcome: str) -> None:
        if self.metrics is not None:
            with contextlib.suppress(Exception):  # metrics never change what a turn does
                self.metrics.record_tool_selection(
                    agent_id=self.agent_id, agent_version=self.agent_version, outcome=outcome
                )

    def note(self, *, fell_back: bool, max_rate: float | None) -> None:
        """One triggered selection finished; `fell_back`: the model needed the full list."""
        self._recent.append(fell_back)
        if (
            max_rate is not None
            and not self.disabled
            and len(self._recent) >= MIN_SAMPLES
            and sum(self._recent) / len(self._recent) > max_rate
        ):
            self.disabled = True
            self.record("auto_disabled")


@dataclass(frozen=True)
class Selection:
    tools: tuple[LLMToolDefinition, ...]  # what the model sees
    applied: bool  # False: the full catalog (not triggered, error, or disabled)

    def removed(self, catalog: tuple[LLMToolDefinition, ...]) -> frozenset[str]:
        kept = {t.name for t in self.tools}
        return frozenset(t.name for t in catalog if t.name not in kept)


def triggered(settings: ToolSelectionSettings, catalog: tuple[LLMToolDefinition, ...]) -> bool:
    if settings.max_tools_per_call is not None and len(catalog) > settings.max_tools_per_call:
        return True
    if settings.max_tool_tokens is not None:
        return sum(estimate_tokens(t) for t in catalog) > settings.max_tool_tokens
    return False


def reduce_catalog(
    settings: ToolSelectionSettings,
    catalog: tuple[LLMToolDefinition, ...],
    kept_names: list[str] | None,
) -> tuple[LLMToolDefinition, ...]:
    """Subset rule: (`kept_names` + always_include) ∩ catalog, in catalog order."""
    always = {c.replace(".", "__") for c in settings.always_include}
    wanted = always | set(kept_names or ())
    return tuple(t for t in catalog if t.name in wanted)


async def decide(
    settings: ToolSelectionSettings,
    selector: ToolSelector,
    monitor: SelectionMonitor,
    message: str,
    catalog: tuple[LLMToolDefinition, ...],
) -> dict[str, Any]:
    """The journaled payload: {"mode": "full"|"selected", "kept": [...], "reason": str}."""
    if monitor.disabled:
        return {"mode": "full", "kept": [], "reason": "auto_disabled"}
    if not triggered(settings, catalog):
        monitor.record("skipped")
        return {"mode": "full", "kept": [], "reason": "below_limits"}
    try:
        ranked = await selector.rank(message, catalog)
    except Exception:  # on_error=full_list: a broken selector never blocks the turn
        monitor.record("provider_error")
        return {"mode": "full", "kept": [], "reason": "provider_error"}
    allowed = {t.name for t in catalog}  # the subset rule: names outside it are ignored
    ordered = sorted(
        (s for s in ranked if s.name in allowed and s.score >= settings.min_score),
        key=lambda s: (-s.score, s.name),
    )
    kept = [s.name for s in ordered[: settings.top_n]]
    if not kept:  # nothing looks relevant: do not hide everything from the model
        monitor.record("provider_error")
        return {"mode": "full", "kept": [], "reason": "no_candidate"}
    monitor.record("applied")
    return {"mode": "selected", "kept": kept, "reason": "over_limit"}
