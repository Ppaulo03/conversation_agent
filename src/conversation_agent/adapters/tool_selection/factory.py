from __future__ import annotations

from conversation_agent.adapters.tool_selection.rules import RulesToolSelector
from conversation_agent.core.definitions.agent import AgentDefinition
from conversation_agent.ports.tool_selector import ToolSelector


def tool_selector_for(agent: AgentDefinition) -> ToolSelector | None:
    """The selector the agent's `tool_selection` asks for (None: selection is off)."""
    sel = agent.tool_selection
    if sel is None or not sel.enabled:
        return None
    if sel.provider == "rules":
        return RulesToolSelector(sel.rules)
    raise ValueError(f"unsupported tool_selection provider {sel.provider!r}")
