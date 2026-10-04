"""AgentDefinition: built directly from typed Python objects (DESIGN §3, ROADMAP Phase 1).

No Pack and no YAML/DSL: those arrive only after this model is proven.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from conversation_agent.core.definitions.binding import CapabilityBinding, ResolvedToolBinding
from conversation_agent.core.definitions.capability import RISK_ORDER, CapabilityDefinition
from conversation_agent.core.definitions.flow import FlowDefinition, Invoke, Propose
from conversation_agent.core.definitions.tool import ToolDefinition
from conversation_agent.core.errors import DefinitionError


class ConfirmationTexts(BaseModel):
    """Deterministic, runtime-owned wording for the confirmation protocol (one language per
    agent). `{summary}` is the rendered action summary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str = "Please confirm: {summary}. Reply YES to confirm or NO to cancel."
    reprompt: str = "I did not catch that. {summary} - reply YES to confirm or NO to cancel."
    rejected: str = "Understood, I cancelled that request."
    expired: str = "That request expired. Tell me again what you would like to do."
    gave_up: str = "I could not get a clear confirmation, so I cancelled the request."
    executed_fallback: str = "Your request was processed (status: {status})."


class AgentDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    agent_id: str
    version: str
    persona: str  # domain prompt text; generic runtime rules are added by the engine
    timezone: str = "UTC"
    capabilities: tuple[CapabilityDefinition, ...]
    tools: tuple[ToolDefinition, ...]
    bindings: tuple[CapabilityBinding, ...]
    allowed_capabilities: frozenset[str]
    max_history_messages: int = 40
    fallback_reply: str = "Sorry, I could not complete that request."
    # Said when a newer message interrupts a turn AFTER something irreversible already happened:
    # the real operation is reported as done, never silently dropped (DESIGN 20.2).
    cancelled_after_effect_reply: str = (
        "Recebi sua nova mensagem. O que já estava em andamento foi concluído; "
        "vou cuidar do seu pedido agora."
    )
    flows: tuple[FlowDefinition, ...] = ()
    max_tokens_per_turn: int | None = None  # guardrail: LLM tokens (in+out) per turn
    confirmation: ConfirmationTexts = ConfirmationTexts()
    confirmation_prompt_enabled: bool = True

    @model_validator(mode="after")
    def _references_are_consistent(self) -> AgentDefinition:
        caps = [c.name for c in self.capabilities]
        tools = [t.name for t in self.tools]
        bound = [b.capability for b in self.bindings]
        for label, names in (("capability", caps), ("tool", tools), ("binding", bound)):
            if len(set(names)) != len(names):
                raise DefinitionError(f"duplicate {label} names in agent {self.agent_id!r}")
        for b in self.bindings:
            if b.capability not in caps:
                raise DefinitionError(f"binding references unknown capability {b.capability!r}")
            if b.tool not in tools:
                raise DefinitionError(f"binding references unknown tool {b.tool!r}")
        by_cap = {c.name: c for c in self.capabilities}
        by_tool = {t.name: t for t in self.tools}
        for b in self.bindings:
            floor = max(
                (by_cap[b.capability].risk, by_tool[b.tool].risk), key=lambda r: RISK_ORDER[r]
            )
            if b.risk is not None and RISK_ORDER[b.risk] < RISK_ORDER[floor]:
                raise DefinitionError(
                    f"binding {b.capability!r}->{b.tool!r} declares risk {b.risk!r}, lower than "
                    f"{floor!r}: a binding can raise protection but never lower it"
                )
        self._check_flows(by_cap)
        self._check_retry_against_effective_risk(by_cap, by_tool)
        self._check_recovery_lookups(by_cap, by_tool)
        for name in self.allowed_capabilities:
            if name not in bound:
                raise DefinitionError(f"allowed capability {name!r} has no binding")
        return self

    def _check_flows(self, by_cap: dict[str, CapabilityDefinition]) -> None:
        """A flow only ever talks to Capabilities this agent has: reads in `Invoke`, protected
        ones in `Propose`, and digressions can use reads only."""
        names = [f.name for f in self.flows]
        if len(set(names)) != len(names):
            raise DefinitionError("duplicate flow names")
        for flow in self.flows:
            for step in flow.steps:
                if isinstance(step, Invoke | Propose):
                    cap = by_cap.get(step.capability)
                    if cap is None or step.capability not in self.allowed_capabilities:
                        raise DefinitionError(
                            f"flow {flow.name!r} step {step.id!r}: capability "
                            f"{step.capability!r} is not defined and allowed"
                        )
                    protected = cap.risk != "read" or cap.confirmation_required
                    if isinstance(step, Invoke) and protected:
                        raise DefinitionError(
                            f"flow {flow.name!r} step {step.id!r}: invoke is for reads; use "
                            f"propose for the protected {step.capability!r}"
                        )
                    if isinstance(step, Propose) and not protected:
                        raise DefinitionError(
                            f"flow {flow.name!r} step {step.id!r}: propose is for protected "
                            f"capabilities; {step.capability!r} is a plain read"
                        )
            for name in flow.digression_capabilities:
                cap = by_cap.get(name)
                if cap is None or cap.risk != "read" or cap.confirmation_required:
                    raise DefinitionError(
                        f"flow {flow.name!r}: a digression may only use read capabilities "
                        f"({name!r} is not)"
                    )

    def _check_retry_against_effective_risk(
        self, by_cap: dict[str, CapabilityDefinition], by_tool: dict[str, ToolDefinition]
    ) -> None:
        """A tool that looks like a read but is bound to a protected capability is a write for
        every runtime purpose, so the write-retry rules (INV-021) apply to it too."""
        for b in self.bindings:
            tool = by_tool[b.tool]
            risks = [by_cap[b.capability].risk, tool.risk] + ([b.risk] if b.risk else [])
            effective = max(risks, key=lambda r: RISK_ORDER[r])
            retry = tool.retry
            unsafe = not tool.idempotency_supported or retry.mode != "same_idempotency_key"
            if effective != "read" and retry.max_attempts > 1 and unsafe:
                raise DefinitionError(
                    f"tool {tool.name!r} is bound to {b.capability!r} (effective risk "
                    f"{effective!r}) and cannot retry without idempotency support and the "
                    "SAME idempotency key"
                )

    def _check_recovery_lookups(
        self, by_cap: dict[str, CapabilityDefinition], by_tool: dict[str, ToolDefinition]
    ) -> None:
        """A status lookup must be a bound read that takes the idempotency key, and its result
        must be convertible into the result of the operation it recovers."""
        bound = {b.capability for b in self.bindings}
        for tool in self.tools:
            recovery = tool.recovery
            if recovery is None or recovery.strategy != "status_lookup":
                continue
            name = recovery.lookup_capability
            lookup = by_cap.get(name or "")
            if lookup is None or name not in bound:
                raise DefinitionError(
                    f"tool {tool.name!r}: lookup capability {name!r} is not defined and bound"
                )
            if lookup.risk != "read":
                raise DefinitionError(f"tool {tool.name!r}: the status lookup must be a read")
            if "idempotency_key" not in lookup.input_model.model_fields:
                raise DefinitionError(
                    f"lookup {name!r} must take an `idempotency_key` (supplied by the runtime)"
                )
            for b in self.bindings:
                if b.tool != tool.name or recovery.result_map is not None:
                    continue
                original = by_cap[b.capability].output_model
                if lookup.output_model.model_json_schema() != original.model_json_schema():
                    raise DefinitionError(
                        f"lookup {name!r} returns a different schema than {b.capability!r}: "
                        "declare `recovery.result_map` to convert it"
                    )

    def resolve(self, capability_name: str) -> ResolvedToolBinding | None:
        binding = next((b for b in self.bindings if b.capability == capability_name), None)
        if binding is None:
            return None
        capability = next(c for c in self.capabilities if c.name == capability_name)
        tool = next(t for t in self.tools if t.name == binding.tool)
        return ResolvedToolBinding(capability=capability, tool=tool, binding=binding)
