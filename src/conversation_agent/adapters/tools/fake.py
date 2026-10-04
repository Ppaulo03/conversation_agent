from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from conversation_agent.core.definitions.binding import ResolvedToolBinding
from conversation_agent.core.models.tooling import ToolContext, ToolResult


@dataclass(frozen=True)
class RecordedCall:
    tool_name: str
    args: dict[str, Any]
    context: ToolContext


class FakeToolProvider:
    """Scripted provider keyed by tool name; records every call it receives."""

    def __init__(self, results: dict[str, ToolResult | Callable[[dict[str, Any]], ToolResult]]):
        self._results = results
        self.calls: list[RecordedCall] = []
        self.fingerprint: str | None = None  # tests can simulate a destination identity
        self.received_fingerprints: list[str | None] = []

    async def destination_fingerprint(
        self, binding: ResolvedToolBinding, context: ToolContext
    ) -> str | None:
        return self.fingerprint

    async def execute(
        self,
        binding: ResolvedToolBinding,
        args: dict[str, Any],
        context: ToolContext,
        *,
        destination_fingerprint: str | None = None,
    ) -> ToolResult:
        self.received_fingerprints.append(destination_fingerprint)
        self.calls.append(RecordedCall(binding.tool.name, dict(args), context))
        result = self._results[binding.tool.name]
        return result(args) if callable(result) else result
