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


class RuntimeMetrics(Protocol):
    """Operational counters of the conversation runtime itself (the hot path)."""

    def record_turn(
        self,
        *,
        agent_id: str,
        agent_version: str,
        outcome: str,
        processing_seconds: float,
        queue_seconds: float | None,
        llm_calls: int,
        proposed: int,
    ) -> None:
        """One finished pass over a turn. `outcome`: completed | failed | retry | waiting |
        cancelled | silent | proactive | blocked. `queue_seconds` is how long the contact's
        oldest message waited before processing began (None when unknown)."""
        ...

    def record_handoff(self, *, agent_id: str, agent_version: str) -> None: ...
