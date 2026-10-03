"""Capability -> Binding -> PolicyGate -> ToolRunner (INV-003).

`evaluate` is pure (no I/O) and `execute` is the only path to an external operation.
Both outputs are journaled by the TurnEngine, so replay never re-evaluates blindly.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from conversation_agent.core.definitions.agent import AgentDefinition
from conversation_agent.core.definitions.binding import ResolvedToolBinding
from conversation_agent.core.models.llm import LLMToolDefinition
from conversation_agent.core.models.runtime import ExecutionIntent
from conversation_agent.core.models.tooling import (
    CapabilityRequest,
    CapabilityResult,
    PolicyDecision,
    ToolContext,
    ToolError,
)
from conversation_agent.engine.policy_gate import PolicyGate
from conversation_agent.engine.tool_runner import ToolRunner
from conversation_agent.tools.intent import binding_fingerprint
from conversation_agent.tools.requests import RequestRejected, build_capability_request

_PROTECTED_NOTE = (
    " PROTECTED: calling this only records a proposal for later confirmation; "
    "it does NOT perform the action."
)


class Evaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    capability: str
    decision: PolicyDecision
    request: CapabilityRequest | None = None
    rejection: ToolError | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Evaluation:
        """deny -> neither; otherwise exactly one of a validated request / a rejection."""
        if self.decision.outcome == "deny":
            if self.request is not None or self.rejection is not None:
                raise ValueError("a denied evaluation carries no request or rejection")
        elif (self.request is None) == (self.rejection is None):
            raise ValueError("a non-denied evaluation needs exactly one of request / rejection")
        return self


class CapabilityOutcome(BaseModel):
    """Exactly one of `result` (executed or refused) / `proposal` (draft, not executed)."""

    model_config = ConfigDict(frozen=True)

    result: CapabilityResult | None = None
    proposal: CapabilityRequest | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> CapabilityOutcome:
        if (self.result is None) == (self.proposal is None):
            raise ValueError("exactly one of result / proposal is required")
        return self


def llm_name_for(capability_name: str) -> str:
    """Same injective mapping as `CapabilityDefinition.llm_name`."""
    return capability_name.replace(".", "__")


class CapabilityPipeline:
    def __init__(self, agent: AgentDefinition, policy: PolicyGate, runner: ToolRunner) -> None:
        self._agent = agent
        self._policy = policy
        self._runner = runner
        self._by_tool_name = {c.llm_name: c.name for c in agent.capabilities}

    def exposed_tools(self) -> tuple[LLMToolDefinition, ...]:
        """Capability schemas (never API schemas) for allowed capabilities only."""
        tools: list[LLMToolDefinition] = []
        for cap in self._agent.capabilities:
            resolved = self._agent.resolve(cap.name)
            if cap.name not in self._agent.allowed_capabilities or resolved is None:
                continue
            decision = self._policy.evaluate(cap.name, resolved)
            note = _PROTECTED_NOTE if decision.outcome == "require_confirmation" else ""
            tools.append(
                LLMToolDefinition(
                    name=cap.llm_name,
                    description=cap.description + note,
                    input_schema=cap.input_model.model_json_schema(),
                )
            )
        return tuple(tools)

    async def run_internal_read(
        self, capability_name: str, raw_args: dict[str, Any], context: ToolContext
    ) -> CapabilityResult:
        """Runtime-initiated READ (e.g. a recovery status lookup). It still goes through
        Capability -> Binding -> ToolRunner, and it refuses anything that needs protection."""
        resolved = self.resolve(capability_name)
        if resolved.effective_risk != "read" or resolved.effective_confirmation_required:
            return CapabilityResult(
                status="policy_denied",
                error=ToolError(code="NOT_A_READ", message_safe="Internal calls must be reads."),
            )
        try:
            request = build_capability_request(resolved.capability, raw_args)
        except RequestRejected as exc:
            return CapabilityResult(status="validation_error", error=exc.error)
        return await self._runner.run(resolved, request, context)

    @property
    def agent_id(self) -> str:
        return self._agent.agent_id

    def freeze(self, request: CapabilityRequest) -> ExecutionIntent | CapabilityResult:
        """Resolve the concrete external operation (no I/O) so it can be persisted at PREPARE."""
        return self._runner.freeze(self.resolve(request.capability), request)

    def intent_matches(self, intent: ExecutionIntent) -> bool:
        """True iff the operation resolved from the *currently deployed* definitions is the one
        that was frozen. A false answer means "do not execute": the world changed (INV-023)."""
        try:
            resolved = self.resolve(intent.capability)
        except KeyError:
            return False
        return binding_fingerprint(resolved) == intent.binding_fingerprint

    async def run_frozen(self, intent: ExecutionIntent, context: ToolContext) -> CapabilityResult:
        """Execute a frozen intent EXACTLY (same tool args, same identity/idempotency key).
        Callers must have checked `intent_matches`."""
        return await self._runner.run_frozen(self.resolve(intent.capability), intent, context)

    def resolve(self, capability_name: str) -> ResolvedToolBinding:
        resolved = self._agent.resolve(capability_name)
        if resolved is None:
            raise KeyError(f"no binding for capability {capability_name!r}")
        return resolved

    def capability_name_for(self, llm_tool_name_: str) -> str:
        """Unknown names are passed through so the PolicyGate denies them."""
        return self._by_tool_name.get(llm_tool_name_, llm_tool_name_)

    def evaluate(self, capability_name: str, raw_args: dict[str, Any]) -> Evaluation:
        resolved = self._agent.resolve(capability_name)
        decision = self._policy.evaluate(capability_name, resolved)
        if decision.outcome == "deny" or resolved is None:
            return Evaluation(capability=capability_name, decision=decision)
        try:
            request = build_capability_request(resolved.capability, raw_args)
        except RequestRejected as exc:
            return Evaluation(capability=capability_name, decision=decision, rejection=exc.error)
        return Evaluation(capability=capability_name, decision=decision, request=request)

    async def execute(self, evaluation: Evaluation, context: ToolContext) -> CapabilityOutcome:
        if evaluation.decision.outcome == "deny":
            return CapabilityOutcome(
                result=CapabilityResult(
                    status="policy_denied",
                    error=ToolError(
                        code=evaluation.decision.reason.upper(),
                        message_safe="This capability is not available.",
                    ),
                )
            )
        if evaluation.rejection is not None or evaluation.request is None:
            return CapabilityOutcome(
                result=CapabilityResult(status="validation_error", error=evaluation.rejection)
            )
        if evaluation.decision.outcome == "require_confirmation":
            return CapabilityOutcome(proposal=evaluation.request)
        resolved = self._agent.resolve(evaluation.capability)
        assert resolved is not None  # allow implies resolvable
        return CapabilityOutcome(
            result=await self._runner.run(resolved, evaluation.request, context)
        )
