from __future__ import annotations

from typing import Any

from conversation_agent.core.definitions.binding import ResolvedToolBinding
from conversation_agent.core.models.tooling import ToolContext, ToolResult
from conversation_agent.ports.tool_provider import ToolProvider


class ExecutionSpy:
    """Remembers WHICH capabilities actually reached a provider (in order), so an eval can assert
    that something was, or was never, executed. It changes nothing about the call."""

    def __init__(self, inner: ToolProvider, executed: list[str]) -> None:
        self._inner = inner
        self._executed = executed  # shared by every provider of one scenario

    async def destination_fingerprint(
        self, binding: ResolvedToolBinding, context: ToolContext
    ) -> str | None:
        return await self._inner.destination_fingerprint(binding, context)

    async def execute(
        self,
        binding: ResolvedToolBinding,
        args: dict[str, Any],
        context: ToolContext,
        *,
        destination_fingerprint: str | None = None,
    ) -> ToolResult:
        self._executed.append(binding.capability.name)
        return await self._inner.execute(
            binding, args, context, destination_fingerprint=destination_fingerprint
        )
