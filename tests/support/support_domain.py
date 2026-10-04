"""The support agent (Phase 9): a second domain, wired over the reference MCP server."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import McpHandle
from conversation_agent.adapters.journal.memory import InMemoryTurnJournal
from conversation_agent.adapters.manifest.yaml_loader import load_manifest_file
from conversation_agent.adapters.tools.mcp import MCPToolProvider
from conversation_agent.core.compiler import CompiledAgent, compile_manifest
from conversation_agent.core.models.connections import ResolvedConnection
from conversation_agent.core.models.conversation import ConversationState, TurnOutcome
from conversation_agent.engine.capability_pipeline import CapabilityPipeline
from conversation_agent.engine.policy_gate import PolicyGate
from conversation_agent.engine.tool_runner import ToolRunner
from conversation_agent.engine.turn_engine import TurnEngine
from conversation_agent.ports.llm import LLMProvider
from support.builders import IDENTITY, new_clock

ROOT = Path(__file__).resolve().parents[2]
SUPPORT_DIR = ROOT / "examples" / "support-agent"
CONNECTION = "support_mcp"


def manifest() -> dict[str, Any]:
    return load_manifest_file(SUPPORT_DIR / "agent.yaml")


def compiled_support() -> CompiledAgent:
    return compile_manifest(manifest())


def provider_for(mcp: McpHandle) -> MCPToolProvider:
    connection = ResolvedConnection(
        connection_id=CONNECTION,
        base_url=mcp.base_url,
        allow_private_networks=True,
        tls_required=False,
    )
    return MCPToolProvider.static({CONNECTION: connection})


def pipeline_for(compiled: CompiledAgent, mcp: McpHandle) -> CapabilityPipeline:
    gate = PolicyGate(compiled.agent.allowed_capabilities)
    return CapabilityPipeline(compiled, gate, ToolRunner({"mcp": provider_for(mcp)}))


class Chat:
    """One conversation with the support agent over the real MCP server."""

    def __init__(self, mcp: McpHandle, llm: LLMProvider) -> None:
        compiled = compiled_support()
        self.llm = llm
        self.engine = TurnEngine(
            compiled,
            llm,
            pipeline_for(compiled, mcp),
            InMemoryTurnJournal(),
            new_clock(),
        )
        self.state = ConversationState()
        self.n = 0

    async def say(self, text: str) -> TurnOutcome:
        self.n += 1
        outcome = await self.engine.process_turn(IDENTITY, self.state, text, f"turn-{self.n}")
        self.state = outcome.state
        return outcome
