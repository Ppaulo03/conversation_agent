"""The eval harness: runs a suite against a compiled agent, offline and deterministic.

Composition root for evals. Per scenario it builds a fresh engine over the REAL pipeline
(compiler-checked agent, policy gate, tool runner) with a scripted model and the providers it is
given (usually replayed cassettes, so no network), and records which capabilities actually reached
a provider. The report carries the digests of the agent and of the suite: that pair is what a
release gate accepts as evidence.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.journal.memory import InMemoryTurnJournal
from conversation_agent.adapters.llm.fake import (
    FakeLLM,
    Script,
    structured_response,
    text_response,
    tool_call_response,
)
from conversation_agent.adapters.llm.metered import MeteredLLMProvider
from conversation_agent.adapters.llm.usage_memory import InMemoryLLMUsageStore
from conversation_agent.adapters.tools.spy import ExecutionSpy
from conversation_agent.core.compiler import CompiledAgent
from conversation_agent.core.definitions.evals import EvalSuite, LLMStep
from conversation_agent.core.models.conversation import ConversationIdentity
from conversation_agent.core.observability import bind
from conversation_agent.core.releases import ReleaseEvidence
from conversation_agent.engine.capability_pipeline import CapabilityPipeline
from conversation_agent.engine.evals import EvalResult, run_scenario
from conversation_agent.engine.policy_gate import PolicyGate
from conversation_agent.engine.tool_runner import ToolRunner
from conversation_agent.engine.turn_engine import TurnEngine
from conversation_agent.ports.tool_provider import ToolProvider

DEFAULT_NOW = datetime(2026, 10, 5, 11, 0, tzinfo=UTC)


@dataclass(frozen=True)
class ScenarioUsage:
    """What a scenario asked of the model: a real, deterministic measure of how chatty the agent
    is (token figures are the SCRIPTED ones, so they only mean something when the suite models
    realistic usage; the number of calls and their purposes always do)."""

    scenario: str
    llm_calls: int
    purposes: dict[str, int]
    tokens: int


@dataclass(frozen=True)
class SuiteReport:
    suite: str
    suite_digest: str
    agent_id: str
    agent_version: str
    agent_digest: str
    results: tuple[EvalResult, ...]
    usage: tuple[ScenarioUsage, ...] = ()

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def passed(self) -> bool:
        return self.passed_count == self.total

    def evidence(self) -> ReleaseEvidence:
        """What a release gate accepts: this suite, run against exactly this agent."""
        return ReleaseEvidence(
            agent_digest=self.agent_digest,
            suite=self.suite,
            suite_digest=self.suite_digest,
            passed=self.passed_count,
            total=self.total,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "suite_digest": self.suite_digest,
            "agent": {
                "id": self.agent_id,
                "version": self.agent_version,
                "digest": self.agent_digest,
            },
            "passed": self.passed,
            "scenarios": [
                {"name": r.scenario, "passed": r.passed, "failures": r.failures}
                for r in self.results
            ],
            "llm_usage": [
                {
                    "scenario": u.scenario,
                    "llm_calls": u.llm_calls,
                    "purposes": u.purposes,
                    "tokens": u.tokens,
                }
                for u in self.usage
            ],
        }


def script_for(steps: list[LLMStep]) -> list[Script]:
    script: list[Script] = []
    for step in steps:
        if step.text is not None:
            script.append(text_response(step.text))
        elif step.tool_call is not None:
            script.append(tool_call_response(step.tool_call.name, dict(step.tool_call.arguments)))
        else:
            assert step.structured is not None
            script.append(structured_response(dict(step.structured)))
    return script


class EvalHarness:
    def __init__(
        self,
        compiled: CompiledAgent,
        providers: Callable[[], Mapping[str, ToolProvider]],
        *,
        now: datetime = DEFAULT_NOW,
    ) -> None:
        self._compiled = compiled
        self._providers = providers  # a FRESH set per scenario: nothing carries over
        self._now = now

    async def run(self, suite: EvalSuite, variables: dict[str, str] | None = None) -> SuiteReport:
        results: list[EvalResult] = []
        usage: list[ScenarioUsage] = []
        merged = {**suite.variables, **(variables or {})}
        for scenario in suite.scenarios:
            executed: list[str] = []
            providers = {k: ExecutionSpy(p, executed) for k, p in self._providers().items()}
            ledger = InMemoryLLMUsageStore()
            llm = MeteredLLMProvider(
                FakeLLM(script_for([s for turn in scenario.turns for s in turn.llm])), ledger
            )
            agent = self._compiled.agent
            pipeline = CapabilityPipeline(
                self._compiled, PolicyGate(agent.allowed_capabilities), ToolRunner(providers)
            )
            engine = TurnEngine(
                self._compiled, llm, pipeline, InMemoryTurnJournal(), FixedClock(self._now)
            )
            identity = ConversationIdentity(
                tenant_id="eval",
                channel_id="eval",
                conversation_id=f"eval-{scenario.name}",
                session_id=f"eval-{scenario.name}",
                contact_id="eval-contact",
            )
            with bind(tenant_id="eval"):  # so the ledger attributes the calls
                results.append(
                    await run_scenario(
                        scenario,
                        engine,
                        identity,
                        turn_prefix=scenario.name,
                        variables=merged,
                        executed=lambda executed=executed: executed,  # type: ignore[misc]
                    )
                )
            purposes = Counter(r.purpose for r in ledger.records)
            usage.append(
                ScenarioUsage(
                    scenario.name,
                    len(ledger.records),
                    dict(purposes),
                    sum(
                        r.input_tokens
                        + r.output_tokens
                        + r.cache_read_tokens
                        + r.cache_write_tokens
                        for r in ledger.records
                    ),
                )
            )
        return SuiteReport(
            suite=suite.suite,
            suite_digest=suite.digest,
            agent_id=self._compiled.agent_id,
            agent_version=self._compiled.version,
            agent_digest=self._compiled.digest,
            results=tuple(results),
            usage=tuple(usage),
        )
