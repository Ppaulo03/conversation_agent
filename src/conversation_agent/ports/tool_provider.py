from __future__ import annotations

from typing import Any, Protocol

from conversation_agent.core.definitions.binding import ResolvedToolBinding
from conversation_agent.core.models.tooling import ToolContext, ToolResult


class ToolProvider(Protocol):
    """Technical execution only.

    Contract: never raises for execution failures; every outcome is a canonical `ToolResult`
    (provider/transport exceptions are converted using the binding's `error_map`). An
    ambiguous write is `unknown`, never a safely-retryable `technical_error`.
    """

    async def destination_fingerprint(
        self, binding: ResolvedToolBinding, context: ToolContext
    ) -> str | None:
        """Identity of the concrete destination this provider would use right now (no secrets),
        or None when the notion does not apply. Frozen at PREPARE (INV-027)."""
        ...

    async def execute(
        self,
        binding: ResolvedToolBinding,
        args: dict[str, Any],
        context: ToolContext,
        *,
        destination_fingerprint: str | None = None,
    ) -> ToolResult:
        """When `destination_fingerprint` is given and no longer matches, nothing is sent and
        the result is a technical_error `DESTINATION_CHANGED`."""
        ...
