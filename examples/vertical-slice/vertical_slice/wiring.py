"""Composition root for the vertical slice: the only place that picks concrete adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from conversation_agent.adapters.clock import SystemClock
from conversation_agent.adapters.journal.memory import InMemoryTurnJournal
from conversation_agent.adapters.tools.http import HTTPToolProvider, local_dev_connection
from conversation_agent.core.definitions.agent import AgentDefinition
from conversation_agent.engine.capability_pipeline import CapabilityPipeline
from conversation_agent.engine.policy_gate import PolicyGate
from conversation_agent.engine.side_effects import ToolStepExecutor
from conversation_agent.engine.tool_runner import ToolRunner
from conversation_agent.engine.turn_engine import TurnEngine
from conversation_agent.ports.clock import Clock
from conversation_agent.ports.journal import TurnJournal
from conversation_agent.ports.llm import LLMProvider
from conversation_agent.ports.tool_provider import ToolProvider
from vertical_slice.definitions import CONNECTION, build_agent


def build_pipeline(
    *,
    api_base_url: str,
    providers: Mapping[str, ToolProvider] | None = None,
    policy: PolicyGate | None = None,
) -> tuple[CapabilityPipeline, AgentDefinition, HTTPToolProvider | None]:
    """Capability -> Binding -> PolicyGate -> ToolRunner for the scheduling agent."""
    agent = build_agent()
    http: HTTPToolProvider | None = None
    if providers is None:
        http = HTTPToolProvider.static({CONNECTION: local_dev_connection(api_base_url)})
        providers = {"http": http}
    gate = policy or PolicyGate(agent.allowed_capabilities)
    return CapabilityPipeline(agent, gate, ToolRunner(providers)), agent, http


def build_engine(
    llm: LLMProvider,
    *,
    api_base_url: str,
    journal: TurnJournal | None = None,
    clock: Clock | None = None,
    providers: Mapping[str, ToolProvider] | None = None,
    policy: PolicyGate | None = None,
    pipeline: CapabilityPipeline | None = None,
    executor_factory: Callable[[CapabilityPipeline], ToolStepExecutor] | None = None,
) -> tuple[TurnEngine, AgentDefinition, HTTPToolProvider | None]:
    http: HTTPToolProvider | None = None
    if pipeline is None:
        pipeline, agent, http = build_pipeline(
            api_base_url=api_base_url, providers=providers, policy=policy
        )
    else:
        agent = build_agent()
    engine = TurnEngine(
        agent,
        llm,
        pipeline,
        journal or InMemoryTurnJournal(),
        clock or SystemClock(agent.timezone),
        tool_executor=executor_factory(pipeline) if executor_factory else None,
    )
    return engine, agent, http
