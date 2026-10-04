"""Two agents installing the SAME Pack over two different scheduling APIs (Phase 8)."""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from conversation_agent.adapters.journal.memory import InMemoryTurnJournal
from conversation_agent.adapters.llm.fake import FakeLLM
from conversation_agent.adapters.manifest.pack_loader import DirectoryPackLoader, load_pack_file
from conversation_agent.adapters.manifest.yaml_loader import load_manifest_file
from conversation_agent.adapters.tools.http import HTTPToolProvider, local_dev_connection
from conversation_agent.core.compiler import CompiledAgent, compile_manifest
from conversation_agent.core.definitions.pack import PackManifest
from conversation_agent.core.packs import PackCatalog
from conversation_agent.engine.capability_pipeline import CapabilityPipeline
from conversation_agent.engine.policy_gate import PolicyGate
from conversation_agent.engine.tool_runner import ToolRunner
from conversation_agent.engine.turn_engine import TurnEngine
from support.builders import new_clock

ROOT = Path(__file__).resolve().parents[2]
PACKS_DIR = ROOT / "packs"
HOSTS_DIR = ROOT / "examples" / "pack-hosts"


@dataclass(frozen=True)
class HostSpec:
    name: str  # manifest file stem
    connection: str  # the connection id its tools use
    fixture: str  # the pytest fixture of ITS scheduling API
    service_word: str  # what a contact calls one of its services
    first_path: str  # the API path its search hits
    write_path: str  # the API path its booking hits


CLINIC = HostSpec("clinic", "scheduling_api", "api", "corte", "/availability", "/bookings")
STUDIO = HostSpec(
    "studio", "agenda_api", "agenda", "massagem", "/v2/agenda/horarios", "/v2/agenda/reservas"
)
HOSTS = (CLINIC, STUDIO)


def load_pack() -> PackManifest:
    return load_pack_file(PACKS_DIR / "generic-scheduling" / "pack.yaml")


def host_manifest(name: str) -> dict[str, Any]:
    return copy.deepcopy(load_manifest_file(HOSTS_DIR / f"{name}.yaml"))


def catalog_for(raw: dict[str, Any]) -> PackCatalog:
    return DirectoryPackLoader(PACKS_DIR).catalog_for(raw.get("packs", []))


def compile_host(
    name: str, mutate: Callable[[dict[str, Any]], None] | None = None
) -> CompiledAgent:
    raw = host_manifest(name)
    if mutate is not None:
        mutate(raw)
    return compile_manifest(raw, catalog_for(raw))


def pipeline_for(compiled: CompiledAgent, base_url: str, connection: str) -> CapabilityPipeline:
    provider = HTTPToolProvider.static({connection: local_dev_connection(base_url)})
    gate = PolicyGate(compiled.agent.allowed_capabilities)
    return CapabilityPipeline(compiled, gate, ToolRunner({"http": provider}))


def eval_engine(compiled: CompiledAgent, base_url: str, connection: str) -> TurnEngine:
    """The real engine over a real API with a model that must never be called: a Flow answers."""
    pipeline = pipeline_for(compiled, base_url, connection)
    return TurnEngine(compiled, FakeLLM([]), pipeline, InMemoryTurnJournal(), new_clock())


def bookings_of(handle: Any) -> dict[str, Any]:
    """What the external system holds (each API keeps its own state, in its own words)."""
    state = handle.state
    return dict(state.bookings if hasattr(state, "bookings") else state.reservas)
