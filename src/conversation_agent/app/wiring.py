"""Domain-agnostic composition helpers any consumer can import from the installed package.

Everything here works with the base install (no PostgreSQL, no Anthropic SDK): load and compile a
manifest, make the HTTP tool provider for local connections, and build an in-memory engine (a
single process, nothing durable: no confirmation, no executor). The durable runtime is
`conversation_agent.app.runtime.Runtime` (extra `postgres`).
"""

from __future__ import annotations

from collections.abc import Mapping

from conversation_agent.adapters.clock import SystemClock
from conversation_agent.adapters.contact_memory.memory import InMemoryContactMemory
from conversation_agent.adapters.journal.memory import InMemoryTurnJournal
from conversation_agent.adapters.manifest.pack_loader import DirectoryPackLoader
from conversation_agent.adapters.manifest.yaml_loader import load_manifest_file
from conversation_agent.adapters.tool_selection.factory import tool_selector_for
from conversation_agent.adapters.tools.guard import GuardedTransport
from conversation_agent.adapters.tools.http import HTTPToolProvider, local_dev_connection
from conversation_agent.core.compiler import CompiledAgent, compile_manifest
from conversation_agent.core.models.audit import configure_pseudonym_key
from conversation_agent.engine.capability_pipeline import CapabilityPipeline
from conversation_agent.engine.policy_gate import PolicyGate
from conversation_agent.engine.tool_runner import ToolRunner
from conversation_agent.engine.turn_engine import TurnEngine
from conversation_agent.ports.clock import Clock
from conversation_agent.ports.contact_memory import ContactMemory
from conversation_agent.ports.llm import LLMProvider
from conversation_agent.ports.metrics import ToolMetrics
from conversation_agent.ports.tool_provider import ToolProvider


def load_agent(manifest: str, packs_dir: str | None = None) -> CompiledAgent:
    """Read a manifest file and compile it (`packs_dir`: where its `packs` are installed from).
    Raises `CompileError` with every diagnostic when it does not compile."""
    raw = load_manifest_file(manifest)
    catalog = None
    if packs_dir is not None:
        declared = raw.get("packs")
        catalog = DirectoryPackLoader(packs_dir).catalog_for(
            declared if isinstance(declared, list) else []
        )
    return compile_manifest(raw, catalog)


def local_http_provider(connections: Mapping[str, str]) -> HTTPToolProvider:
    """`{connection id: base URL}` -> an HTTP tool provider for LOCAL DEVELOPMENT ONLY (private
    networks and plain HTTP allowed). Production resolves connections through a resolver."""
    return HTTPToolProvider.static(
        {name: local_dev_connection(url, name) for name, url in connections.items()}
    )


def build_memory_engine(
    compiled: CompiledAgent,
    llm: LLMProvider,
    *,
    providers: Mapping[str, ToolProvider],
    policy: PolicyGate | None = None,
    clock: Clock | None = None,
    tool_selection_guard: GuardedTransport | None = None,
    tool_metrics: ToolMetrics | None = None,
    contact_memory: ContactMemory | None = None,
) -> TurnEngine:
    """An engine with in-memory state for one process. Protected actions stop at the proposal
    (nothing executes without the durable runtime's confirmation); reads work end to end."""
    agent = compiled.agent
    pipeline = CapabilityPipeline(
        compiled, policy or PolicyGate(agent.allowed_capabilities), ToolRunner(providers)
    )
    return TurnEngine(
        compiled,
        llm,
        pipeline,
        InMemoryTurnJournal(),
        clock or SystemClock(agent.timezone),
        tool_selector=tool_selector_for(agent, guard=tool_selection_guard, metrics=tool_metrics),
        contact_memory=(contact_memory or InMemoryContactMemory())
        if agent.memory is not None
        else None,
    )


def configure_pseudonyms_from_env(env: Mapping[str, str]) -> bool:
    """Read `PSEUDONYM_KEY` (>= 32 bytes) for audit and erasure references. False when unset: the
    code that needs a reference then refuses to produce one."""
    key = env.get("PSEUDONYM_KEY")
    if not key:
        return False
    configure_pseudonym_key(key)
    return True
