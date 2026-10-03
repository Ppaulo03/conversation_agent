"""AgentDefinition: built directly from typed Python objects (DESIGN §3, ROADMAP Phase 1).

No Pack and no YAML/DSL: those arrive only after this model is proven.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from conversation_agent.core.definitions.binding import CapabilityBinding, ResolvedToolBinding
from conversation_agent.core.definitions.capability import CapabilityDefinition
from conversation_agent.core.definitions.tool import ToolDefinition
from conversation_agent.core.errors import DefinitionError


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
        for name in self.allowed_capabilities:
            if name not in bound:
                raise DefinitionError(f"allowed capability {name!r} has no binding")
        return self

    def resolve(self, capability_name: str) -> ResolvedToolBinding | None:
        binding = next((b for b in self.bindings if b.capability == capability_name), None)
        if binding is None:
            return None
        capability = next(c for c in self.capabilities if c.name == capability_name)
        tool = next(t for t in self.tools if t.name == binding.tool)
        return ResolvedToolBinding(capability=capability, tool=tool, binding=binding)
