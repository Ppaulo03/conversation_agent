"""Tool discovery -> Tool Definitions, through an explicit allowlist (DESIGN §32).

    discovered (UNTRUSTED)  +  allowlist (the operator's decision)  ->  ToolManifest(s)

  - DISCOVERY IS NOT EXPOSURE (INV-037): a tool the server offers but the allowlist does not name
    is never imported, whatever its description says about itself.
  - The RISK is the allowlist's, always explicit. A server's own hints (`readOnlyHint`, ...) are
    claims from the very system being trusted: they can only make us MORE careful (a tool the
    server says is not read-only cannot be allowed as `read` without saying so on purpose).
  - A schema this framework cannot represent exactly is refused, never approximated.
  - The input schema is PINNED (`MCPToolSpec.schema_digest`): the tool allowed is the tool whose
    schema was reviewed; a changed schema stops the call (see the MCP provider).
  - Bindings and capabilities stay decisions of the agent: importing a tool grants nothing.

Pure functions over data; the network side (listing a server's tools) lives in the adapter.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from conversation_agent.core.definitions.capability import Risk
from conversation_agent.core.definitions.json_schema import (
    UnsupportedSchemaError,
    clean_text,
    model_spec_from_json_schema,
    schema_digest,
)
from conversation_agent.core.definitions.manifest import ToolManifest
from conversation_agent.core.definitions.schema_spec import ModelSpec
from conversation_agent.core.definitions.tool import MCPToolSpec, RecoverySpec, RetryPolicy

_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DiscoveredTool(_Frozen):
    """What a server says about one of its tools: everything here is prose or claims."""

    name: str
    description: str = ""
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] = Field(default_factory=dict)


class AllowedTool(_Frozen):
    """One tool the operator allows, with every decision the server cannot make for us."""

    remote_name: str
    risk: Risk  # REQUIRED: there is no default that is right for an unknown tool
    name: str | None = None  # local tool name; default `<prefix>_<remote_name>`
    description: str | None = None  # default: the server's, cut and cleaned
    confirmation_required: bool = False
    idempotency_supported: bool = False
    timeout_seconds: float = 5.0
    retry: RetryPolicy = RetryPolicy()
    recovery: RecoverySpec | None = None
    output: ModelSpec | None = None  # default: the server's `outputSchema`, when convertible
    # The server claims the tool is not read-only and the allowlist says `read`: only an explicit
    # decision can override a claim that points toward danger.
    accept_server_hint_conflict: bool = False

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str | None) -> str | None:
        if value is not None and not _TOOL_NAME.fullmatch(value):
            raise ValueError(f"tool name {value!r} must be lower_snake_case")
        return value


class ToolAllowlist(_Frozen):
    connection: str
    prefix: str = "mcp"
    tools: tuple[AllowedTool, ...]

    @field_validator("tools")
    @classmethod
    def _unique(cls, value: tuple[AllowedTool, ...]) -> tuple[AllowedTool, ...]:
        names = [t.remote_name for t in value]
        if len(set(names)) != len(names):
            raise ValueError("the allowlist names the same tool twice")
        return value


class ImportDiagnostic(_Frozen):
    code: str
    tool: str
    message: str


class ImportReport(_Frozen):
    tools: tuple[ToolManifest, ...]
    diagnostics: tuple[ImportDiagnostic, ...]
    not_exposed: tuple[str, ...]  # offered by the server, not allowed: reported, never imported

    @property
    def ok(self) -> bool:
        return not self.diagnostics


def local_name(prefix: str, remote: str) -> str:
    """A safe local tool name from a stranger's identifier."""
    return re.sub(r"[^a-z0-9_]", "_", f"{prefix}_{remote}".lower())


def _server_says_unsafe(annotations: dict[str, Any]) -> bool:
    return annotations.get("readOnlyHint") is False or annotations.get("destructiveHint") is True


def import_mcp_tools(discovered: list[DiscoveredTool], allowlist: ToolAllowlist) -> ImportReport:
    offered = {t.name: t for t in discovered}
    imported: list[ToolManifest] = []
    problems: list[ImportDiagnostic] = []
    for allowed in allowlist.tools:
        found = offered.get(allowed.remote_name)
        if found is None:
            problems.append(
                ImportDiagnostic(
                    code="ALLOWED_TOOL_NOT_OFFERED",
                    tool=allowed.remote_name,
                    message="the allowlist names a tool the server does not offer",
                )
            )
            continue
        if (
            allowed.risk == "read"
            and _server_says_unsafe(found.annotations)
            and not allowed.accept_server_hint_conflict
        ):
            problems.append(
                ImportDiagnostic(
                    code="RISK_CONFLICTS_WITH_SERVER_HINT",
                    tool=allowed.remote_name,
                    message="the server says this tool is not read-only but the allowlist says "
                    "`read`: set `accept_server_hint_conflict` only if that is a reviewed decision",
                )
            )
            continue
        try:
            spec = model_spec_from_json_schema(found.input_schema)
            output = allowed.output or _convert_output(found.output_schema)
        except UnsupportedSchemaError as exc:
            problems.extend(
                ImportDiagnostic(code="UNSUPPORTED_SCHEMA", tool=allowed.remote_name, message=m)
                for m in exc.problems
            )
            continue
        imported.append(
            ToolManifest(
                name=allowed.name or local_name(allowlist.prefix, allowed.remote_name),
                description=allowed.description or clean_text(found.description) or "MCP tool",
                input=spec,
                output=output,
                risk=allowed.risk,
                confirmation_required=allowed.confirmation_required,
                timeout_seconds=allowed.timeout_seconds,
                provider="mcp",
                connection=allowlist.connection,
                mcp=MCPToolSpec(
                    remote_name=found.name, schema_digest=schema_digest(found.input_schema)
                ),
                idempotency_supported=allowed.idempotency_supported,
                retry=allowed.retry,
                recovery=allowed.recovery,
            )
        )
    taken: dict[str, str] = {}
    for manifest in imported:
        if manifest.name in taken:
            problems.append(
                ImportDiagnostic(
                    code="DUPLICATE_TOOL_NAME",
                    tool=manifest.name,
                    message=f"two allowed tools would both be named {manifest.name!r}",
                )
            )
        taken[manifest.name] = manifest.name
    allowed_names = {t.remote_name for t in allowlist.tools}
    return ImportReport(
        tools=tuple(imported),
        diagnostics=tuple(problems),
        not_exposed=tuple(sorted(n for n in offered if n not in allowed_names)),
    )


def _convert_output(schema: dict[str, Any] | None) -> ModelSpec | None:
    """A server's `outputSchema` is a convenience: when it cannot be represented the tool simply
    has no declared output (the binding then has nothing to type-check against), it is not an
    error and it is never approximated."""
    if schema is None:
        return None
    try:
        return model_spec_from_json_schema(schema)
    except UnsupportedSchemaError:
        return None
