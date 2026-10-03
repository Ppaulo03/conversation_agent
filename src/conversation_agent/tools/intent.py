"""Freezing the concrete external operation at PREPARE (INV-023)."""

from __future__ import annotations

from typing import Any

from conversation_agent.core.canonical import stable_hash
from conversation_agent.core.definitions.binding import ResolvedToolBinding
from conversation_agent.core.definitions.tool import RecoverySpec
from conversation_agent.core.models.runtime import ExecutionIntent, RecoverySnapshot


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _dump(v) for k, v in value.items()}
    return value


def binding_fingerprint(resolved: ResolvedToolBinding) -> str:
    """Identity of everything that decides *what is sent where*: the endpoint, provider,
    connection, risk/idempotency flags and the input mapping. Output/error mapping and the
    recovery contract only decide how a result is read, so they are deliberately excluded."""
    tool, binding = resolved.tool, resolved.binding
    return stable_hash(
        resolved.capability.name,
        tool.name,
        tool.provider,
        tool.connection,
        _dump(tool.http),
        tool.risk,
        tool.idempotency_supported,
        _dump(binding.input_map),
    )


def recovery_snapshot(resolved: ResolvedToolBinding) -> RecoverySnapshot:
    spec = resolved.tool.recovery or RecoverySpec(
        strategy="safe_retry" if resolved.effective_risk == "read" else "human_handoff"
    )
    return RecoverySnapshot(
        strategy=spec.strategy,
        lookup_capability=spec.lookup_capability,
        absent_codes=spec.absent_codes,
        idempotency_supported=resolved.tool.idempotency_supported,
    )


def build_intent(resolved: ResolvedToolBinding, tool_args: dict[str, Any]) -> ExecutionIntent:
    return ExecutionIntent(
        capability=resolved.capability.name,
        tool_name=resolved.tool.name,
        provider=resolved.tool.provider,
        connection=resolved.tool.connection,
        tool_args=tool_args,
        tool_args_hash=stable_hash(tool_args),
        binding_fingerprint=binding_fingerprint(resolved),
        recovery=recovery_snapshot(resolved),
    )
