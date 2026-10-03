"""ToolRunner: the only engine component that talks to ToolProviders (INV-003).

Capability request -> input_map -> Tool -> Provider -> output_map -> Capability result.
The Flow/LLM only ever sees Capability schemas.

Two phases, so that an operation can be frozen between them (INV-023):

  freeze()      capability args -> concrete tool args (input mapping + tool schema), no I/O
  run_frozen()  concrete tool args -> provider -> output mapping
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from conversation_agent.core.definitions.binding import ResolvedToolBinding
from conversation_agent.core.errors import MappingError
from conversation_agent.core.models.runtime import ExecutionIntent
from conversation_agent.core.models.tooling import (
    CapabilityRequest,
    CapabilityResult,
    ToolContext,
    ToolError,
    ToolResult,
)
from conversation_agent.ports.tool_provider import ToolProvider
from conversation_agent.tools.intent import build_intent
from conversation_agent.tools.mapping import TRANSFORMS, apply_mapping


def _failure_before_io(code: str, message: str) -> CapabilityResult:
    """Nothing was sent to the external system, so non-execution is *known*: a plain
    technical_error for any risk (never `unknown`, which would trigger pointless reconciliation)."""
    return CapabilityResult(
        status="technical_error",
        error=ToolError(code=code, message_safe=message, retryable=False),
    )


def _failure_after_possible_io(
    resolved: ResolvedToolBinding, code: str, message: str
) -> CapabilityResult:
    """The call may have reached the system: a possible write is `unknown`, a read is
    `technical_error`."""
    status = "technical_error" if resolved.effective_risk == "read" else "unknown"
    return CapabilityResult(
        status=status, error=ToolError(code=code, message_safe=message, retryable=False)
    )


class ToolRunner:
    def __init__(
        self,
        providers: Mapping[str, ToolProvider],
        transforms: Mapping[str, Callable[[Any], Any]] = TRANSFORMS,
    ) -> None:
        self._providers = providers
        self._transforms = transforms

    def freeze(
        self, resolved: ResolvedToolBinding, request: CapabilityRequest
    ) -> ExecutionIntent | CapabilityResult:
        """Resolve the concrete operation without touching the outside world. A mapping defect
        is returned as a (pre-I/O) failure: such a request must never be prepared."""
        try:
            mapped = apply_mapping(resolved.binding.input_map, request.args, self._transforms)
            tool_args = resolved.tool.input_model.model_validate(mapped).model_dump(mode="json")
        except (MappingError, ValidationError):
            # The capability args were already valid, so this is a binding defect, and
            # nothing was sent to the external system.
            return _failure_before_io(
                "BINDING_INPUT_MAPPING_FAILED",
                "The request could not be adapted to the external tool.",
            )
        return build_intent(resolved, tool_args)

    async def run(
        self, resolved: ResolvedToolBinding, request: CapabilityRequest, context: ToolContext
    ) -> CapabilityResult:
        intent = self.freeze(resolved, request)
        if isinstance(intent, CapabilityResult):
            return intent
        return await self.run_frozen(resolved, intent, context)

    async def run_frozen(
        self, resolved: ResolvedToolBinding, intent: ExecutionIntent, context: ToolContext
    ) -> CapabilityResult:
        provider = self._providers.get(resolved.tool.provider)
        if provider is None:
            return _failure_before_io("PROVIDER_NOT_CONFIGURED", "No provider for this tool.")
        try:
            result = await provider.execute(resolved, dict(intent.tool_args), context)
        except Exception:  # provider contract violation: never leak, never assume safe
            return _failure_after_possible_io(
                resolved, "PROVIDER_CONTRACT_VIOLATION", "Tool provider failed."
            )
        return self._to_capability_result(resolved, result)

    def _to_capability_result(
        self, resolved: ResolvedToolBinding, result: ToolResult
    ) -> CapabilityResult:
        if result.status != "success":
            return CapabilityResult(
                status=result.status, error=result.error, provider_metadata=result.provider_metadata
            )
        try:
            if resolved.tool.output_model is not None:
                resolved.tool.output_model.model_validate(result.data)
            mapped = apply_mapping(resolved.binding.output_map, result.data, self._transforms)
            data = resolved.capability.output_model.model_validate(mapped).model_dump(mode="json")
        except (MappingError, ValidationError):
            failure = _failure_after_possible_io(
                resolved,
                "BINDING_OUTPUT_MAPPING_FAILED",
                "The external system answered in an unexpected format.",
            )
            return failure.model_copy(update={"provider_metadata": result.provider_metadata})
        return CapabilityResult(
            status="success", data=data, provider_metadata=result.provider_metadata
        )
