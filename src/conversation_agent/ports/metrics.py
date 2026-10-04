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


class LLMMetrics(Protocol):
    """Operational counters for LLM calls (labels are names, never content or tenant ids unless
    the deployment asks for them)."""

    def record_llm_call(
        self,
        *,
        provider: str,
        model: str,
        purpose: str,
        outcome: str,
        error_code: str | None,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        duration_seconds: float,
        cost_usd: float | None,
    ) -> None:
        """`cost_usd` None: the model has no price (counted as unpriced, never as free)."""
        ...

    def record_usage_write_failure(self) -> None: ...
