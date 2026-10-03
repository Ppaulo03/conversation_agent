from __future__ import annotations

from typing import Any

from conversation_agent.core.definitions.binding import ResolvedToolBinding
from conversation_agent.core.models.tooling import ToolContext, ToolResult
from conversation_agent.ports.faults import FaultInjector
from conversation_agent.ports.tool_provider import ToolProvider


class FaultInjectingToolProvider:
    """Crashes *after* the inner provider executed (the request reached the external system)
    but before the caller sees the response: C05_after_external_send."""

    def __init__(self, inner: ToolProvider, faults: FaultInjector, point: str) -> None:
        self._inner = inner
        self._faults = faults
        self._point = point

    async def execute(
        self, binding: ResolvedToolBinding, args: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        result = await self._inner.execute(binding, args, context)
        await self._faults.hit(self._point)
        return result
