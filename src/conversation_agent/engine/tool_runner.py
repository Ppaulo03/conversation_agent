"""ToolRunner: the only engine component that talks to ToolProviders (INV-003).

Capability request -> input_map -> Tool -> Provider -> output_map -> Capability result.
The Flow/LLM only ever sees Capability schemas.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from conversation_agent.core.definitions.binding import ResolvedToolBinding
from conversation_agent.core.errors import MappingError
from conversation_agent.core.models.tooling import (
    CapabilityRequest,
    CapabilityResult,
    ToolContext,
    ToolError,
    ToolResult,
)
from conversation_agent.ports.tool_provider import ToolProvider
from conversation_agent.tools.mapping import TRANSFORMS, apply_mapping


def _failure(resolved: ResolvedToolBinding, code: str, message: str) -> CapabilityResult:
    """Internal/contract failures: a possible write is `unknown`, a read is `technical_error`."""
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

    async def run(
        self, resolved: ResolvedToolBinding, request: CapabilityRequest, context: ToolContext
    ) -> CapabilityResult:
        provider = self._providers.get(resolved.tool.provider)
        if provider is None:
            return _failure(resolved, "PROVIDER_NOT_CONFIGURED", "No provider for this tool.")

        try:
            mapped = apply_mapping(resolved.binding.input_map, request.args, self._transforms)
            tool_args = resolved.tool.input_model.model_validate(mapped).model_dump(mode="json")
        except (MappingError, ValidationError):
            # The capability args were already valid, so this is a binding defect, and
            # nothing was sent to the external system.
            return CapabilityResult(
                status="technical_error",
                error=ToolError(
                    code="BINDING_INPUT_MAPPING_FAILED",
                    message_safe="The request could not be adapted to the external tool.",
                ),
            )

        try:
            result = await provider.execute(resolved, tool_args, context)
        except Exception:  # provider contract violation: never leak, never assume safe
            return _failure(resolved, "PROVIDER_CONTRACT_VIOLATION", "Tool provider failed.")

        return self._to_capability_result(resolved, result)

    def _to_capability_result(
        self, resolved: ResolvedToolBinding, result: ToolResult
    ) -> CapabilityResult:
        if result.status != "success":
            return CapabilityResult(status=result.status, error=result.error)
        try:
            if resolved.tool.output_model is not None:
                resolved.tool.output_model.model_validate(result.data)
            mapped = apply_mapping(resolved.binding.output_map, result.data, self._transforms)
            data = resolved.capability.output_model.model_validate(mapped).model_dump(mode="json")
        except (MappingError, ValidationError):
            return _failure(
                resolved,
                "BINDING_OUTPUT_MAPPING_FAILED",
                "The external system answered in an unexpected format.",
            )
        return CapabilityResult(status="success", data=data)
