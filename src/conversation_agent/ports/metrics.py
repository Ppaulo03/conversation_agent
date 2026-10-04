from __future__ import annotations

from typing import Protocol


class ToolMetrics(Protocol):
    """Operational counters for outbound tool calls (Phase 10 wires a real backend).

    Implementations must never raise into the caller and must not receive arguments, results or
    secrets: labels are names and canonical codes only."""

    def record_call(
        self,
        *,
        provider: str,
        tool: str,
        status: str,
        error_code: str | None,
        duration_ms: float,
    ) -> None: ...

    def record_circuit(self, *, provider: str, scope: str, state: str) -> None:
        """A circuit changed state (`open`, `half_open`, `closed`)."""
        ...
