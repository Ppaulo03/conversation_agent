from __future__ import annotations

from conversation_agent.adapters.tool_selection.http import HttpToolSelector
from conversation_agent.adapters.tool_selection.rules import RulesToolSelector
from conversation_agent.adapters.tools.guard import GuardedTransport
from conversation_agent.core.definitions.agent import AgentDefinition
from conversation_agent.ports.tool_selector import ToolSelector


def tool_selector_for(
    agent: AgentDefinition, *, guard: GuardedTransport | None = None
) -> ToolSelector | None:
    """The selector the agent's `tool_selection` asks for (None: selection is off). The `http`
    provider needs the deployment's `GuardedTransport` (its connections and secrets)."""
    sel = agent.tool_selection
    if sel is None or not sel.enabled:
        return None
    if sel.provider == "rules":
        return RulesToolSelector(sel.rules)
    if sel.provider == "http" and sel.http is not None:
        if guard is None:
            raise ValueError("tool_selection provider 'http' needs a GuardedTransport (guard=)")
        return HttpToolSelector(guard, sel.http, top_n=sel.top_n)
    raise ValueError(f"unsupported tool_selection provider {sel.provider!r}")
