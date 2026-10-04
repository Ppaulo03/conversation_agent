"""Capability Binding: adapts a Capability to a concrete Tool (DESIGN §6)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from conversation_agent.core.definitions.capability import (
    RISK_ORDER,
    CapabilityDefinition,
    Risk,
)
from conversation_agent.core.definitions.mapping import MappingSpec
from conversation_agent.core.definitions.tool import ToolDefinition

ErrorType = Literal["validation_error", "business_error", "technical_error", "timeout", "unknown"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ErrorRule(_Frozen):
    type: ErrorType
    code: str | None = None
    retryable: bool = False


class ErrorMap(_Frozen):
    """Maps provider outcomes to the canonical taxonomy.

    The rules can never turn an ambiguous write into a safe-retry error: that coercion is
    enforced where the map is applied (`tools.error_mapping`), not trusted from here.
    """

    http_status: dict[int, ErrorRule] = Field(default_factory=dict)
    default_5xx: dict[Literal["read", "write"], ErrorRule] = Field(default_factory=dict)
    timeout: dict[Literal["read", "write"], ErrorRule] = Field(default_factory=dict)


class CapabilityBinding(_Frozen):
    capability: str
    tool: str
    input_map: MappingSpec
    output_map: MappingSpec
    error_map: ErrorMap = Field(default_factory=ErrorMap)
    risk: Risk | None = None
    confirmation_required: bool = False


class ResolvedToolBinding(BaseModel):
    """Everything a provider/runner needs about one resolved capability call."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    capability: CapabilityDefinition
    tool: ToolDefinition
    binding: CapabilityBinding

    @property
    def effective_risk(self) -> Risk:
        """max(capability.risk, tool.risk, binding.risk): a binding never lowers protection."""
        risks: list[Risk] = [self.capability.risk, self.tool.risk]
        if self.binding.risk is not None:
            risks.append(self.binding.risk)
        return max(risks, key=lambda r: RISK_ORDER[r])

    @property
    def requires_protection(self) -> bool:
        """THE rule for "is this a protected operation?": every layer (policy, flows, recovery)
        asks the resolved binding, never a label on one abstraction."""
        return self.effective_risk != "read" or self.effective_confirmation_required

    @property
    def effective_confirmation_required(self) -> bool:
        return (
            self.capability.confirmation_required
            or self.tool.confirmation_required
            or self.binding.confirmation_required
        )
