"""AgentManifest: the serializable form of an agent (YAML/JSON), Phase 5.

It is the SAME model as the Python objects wherever those are already data (bindings, mapping,
flows, recovery, retry, confirmation texts); only Pydantic classes are replaced by `ModelSpec`s.
Loading never executes anything: parsing a manifest builds definitions, nothing more.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from conversation_agent.core.definitions.agent import (
    AgentDefinition,
    ConfirmationTexts,
    HumanRequest,
    MediaTexts,
)
from conversation_agent.core.definitions.binding import CapabilityBinding
from conversation_agent.core.definitions.capability import CapabilityDefinition, Risk
from conversation_agent.core.definitions.flow import FlowDefinition
from conversation_agent.core.definitions.pack import PackLock, PackUse
from conversation_agent.core.definitions.schema_spec import ModelSpec, build_model
from conversation_agent.core.definitions.tool import (
    HTTPRequestSpec,
    MCPToolSpec,
    RecoverySpec,
    RetryPolicy,
    ToolDefinition,
)
from conversation_agent.core.versioning import MANIFEST_SCHEMA_VERSION


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class CapabilityManifest(_Strict):
    name: str
    description: str
    input: ModelSpec
    output: ModelSpec
    risk: Risk = "read"
    confirmation_required: bool = False
    summary_template: str | None = None
    executed_template: str | None = None


class ToolManifest(_Strict):
    name: str
    description: str
    input: ModelSpec
    output: ModelSpec | None = None
    risk: Risk = "read"
    confirmation_required: bool = False
    timeout_seconds: float = 5.0
    provider: str
    connection: str | None = None
    http: HTTPRequestSpec | None = None
    mcp: MCPToolSpec | None = None
    idempotency_supported: bool = False
    retry: RetryPolicy = RetryPolicy()
    recovery: RecoverySpec | None = None


class AgentManifest(_Strict):
    schema_version: int = MANIFEST_SCHEMA_VERSION
    min_framework: str = "0.1.0"  # oldest framework that may run this manifest
    agent_id: str
    version: str  # MAJOR.MINOR.PATCH
    persona: str
    timezone: str = "UTC"
    capabilities: tuple[CapabilityManifest, ...]
    tools: tuple[ToolManifest, ...]
    bindings: tuple[CapabilityBinding, ...]
    allowed_capabilities: tuple[str, ...]
    flows: tuple[FlowDefinition, ...] = ()
    human_request: HumanRequest | None = None
    max_history_messages: int = 40
    fallback_reply: str = "Sorry, I could not complete that request."
    max_tokens_per_turn: int | None = None
    max_flow_depth: int = 3
    confirmation: ConfirmationTexts = Field(default_factory=ConfirmationTexts)
    confirmation_prompt_enabled: bool = True
    media: MediaTexts = Field(default_factory=MediaTexts)
    max_media_bytes: int = 25 * 1024 * 1024
    cancelled_after_effect_reply: str | None = None  # None: the framework default
    # Packs to install (input) and what was installed (provenance, set by the compiler). The
    # runtime never reads either: installation turns Pack content into ordinary definitions.
    packs: tuple[PackUse, ...] = ()
    pack_lock: tuple[PackLock, ...] = ()


def _model_name(name: str, suffix: str) -> str:
    return "".join(part.capitalize() for part in name.replace(".", "_").split("_")) + suffix


def build_tool(t: ToolManifest) -> ToolDefinition:
    """One tool manifest -> its runtime definition (also what the importers emit)."""
    return ToolDefinition(
        name=t.name,
        description=t.description,
        input_model=build_model(_model_name(t.name, "Args"), t.input),
        output_model=build_model(_model_name(t.name, "Response"), t.output, strict=False)
        if t.output is not None
        else None,
        risk=t.risk,
        confirmation_required=t.confirmation_required,
        timeout_seconds=t.timeout_seconds,
        provider=t.provider,
        connection=t.connection,
        http=t.http,
        mcp=t.mcp,
        idempotency_supported=t.idempotency_supported,
        retry=t.retry,
        recovery=t.recovery,
    )


def build_definition(manifest: AgentManifest) -> AgentDefinition:
    """Manifest -> the runtime objects (validates everything AgentDefinition validates)."""
    capabilities = tuple(
        CapabilityDefinition(
            name=c.name,
            description=c.description,
            input_model=build_model(_model_name(c.name, "Input"), c.input),
            output_model=build_model(_model_name(c.name, "Output"), c.output),
            risk=c.risk,
            confirmation_required=c.confirmation_required,
            summary_template=c.summary_template,
            executed_template=c.executed_template,
        )
        for c in manifest.capabilities
    )
    tools = tuple(build_tool(t) for t in manifest.tools)
    extra: dict[str, Any] = {}
    if manifest.human_request is not None:
        extra["human_request"] = manifest.human_request
    if manifest.cancelled_after_effect_reply is not None:
        extra["cancelled_after_effect_reply"] = manifest.cancelled_after_effect_reply
    return AgentDefinition(
        agent_id=manifest.agent_id,
        version=manifest.version,
        persona=manifest.persona,
        timezone=manifest.timezone,
        capabilities=capabilities,
        tools=tools,
        bindings=manifest.bindings,
        allowed_capabilities=frozenset(manifest.allowed_capabilities),
        flows=manifest.flows,
        max_history_messages=manifest.max_history_messages,
        fallback_reply=manifest.fallback_reply,
        max_tokens_per_turn=manifest.max_tokens_per_turn,
        max_flow_depth=manifest.max_flow_depth,
        confirmation=manifest.confirmation,
        confirmation_prompt_enabled=manifest.confirmation_prompt_enabled,
        media=manifest.media,
        max_media_bytes=manifest.max_media_bytes,
        **extra,
    )
