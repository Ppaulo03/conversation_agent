"""Composition root for the vertical slice: the only place that picks concrete adapters."""

from __future__ import annotations

from collections.abc import Mapping

from conversation_agent.adapters.clock import SystemClock
from conversation_agent.adapters.journal.memory import InMemoryTurnJournal
from conversation_agent.adapters.tools.http import HTTPConnection, HTTPToolProvider
from conversation_agent.core.definitions.agent import AgentDefinition
from conversation_agent.engine.capability_pipeline import CapabilityPipeline
from conversation_agent.engine.policy_gate import PolicyGate
from conversation_agent.engine.tool_runner import ToolRunner
from conversation_agent.engine.turn_engine import TurnEngine
from conversation_agent.ports.clock import Clock
from conversation_agent.ports.journal import TurnJournal
from conversation_agent.ports.llm import LLMProvider
from conversation_agent.ports.tool_provider import ToolProvider
from vertical_slice.definitions import CONNECTION, build_agent


def build_engine(
    llm: LLMProvider,
    *,
    api_base_url: str,
    journal: TurnJournal | None = None,
    clock: Clock | None = None,
    providers: Mapping[str, ToolProvider] | None = None,
) -> tuple[TurnEngine, AgentDefinition, HTTPToolProvider | None]:
    agent = build_agent()
    http: HTTPToolProvider | None = None
    if providers is None:
        http = HTTPToolProvider({CONNECTION: HTTPConnection(base_url=api_base_url)})
        providers = {"http": http}
    pipeline = CapabilityPipeline(
        agent, PolicyGate(agent.allowed_capabilities), ToolRunner(providers)
    )
    engine = TurnEngine(
        agent,
        llm,
        pipeline,
        journal or InMemoryTurnJournal(),
        clock or SystemClock(agent.timezone),
    )
    return engine, agent, http
