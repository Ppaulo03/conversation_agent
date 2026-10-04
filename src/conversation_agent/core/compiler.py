"""The agent compiler (Phase 5): everything that can be wrong with an agent is found BEFORE it runs.

    manifest (YAML/JSON dict)  ->  parse  ->  reference/risk/version checks
                               ->  build definitions  ->  mapping/error_map/flow checks
                               ->  CompiledAgent (content digest, immutable)

Diagnostics are collected (one pass reports every problem, each with a stable `code` and a
`path`), never "first error wins". A Python-built `AgentDefinition` goes through the same
post-build checks with `compile_agent`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import UnionType
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from conversation_agent.core.canonical import stable_hash
from conversation_agent.core.definitions.agent import AgentDefinition
from conversation_agent.core.definitions.binding import CapabilityBinding, ResolvedToolBinding
from conversation_agent.core.definitions.capability import RISK_ORDER
from conversation_agent.core.definitions.flow import Invoke, Propose, Slot
from conversation_agent.core.definitions.manifest import AgentManifest, build_definition
from conversation_agent.core.definitions.mapping import (
    TRANSFORM_NAMES,
    Const,
    Each,
    MapExpr,
    Ref,
)
from conversation_agent.core.definitions.schema_spec import schema_fingerprint
from conversation_agent.core.errors import DefinitionError
from conversation_agent.core.versioning import (
    COMPILER_VERSION,
    FRAMEWORK_VERSION,
    MANIFEST_SCHEMA_VERSION,
    Version,
)

SUPPORTED_SCHEMA_VERSIONS = frozenset({1})


@dataclass(frozen=True)
class Diagnostic:
    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.path}: {self.message}"


class CompileError(DefinitionError):
    """One or more problems; `diagnostics` lists them all."""

    def __init__(self, diagnostics: list[Diagnostic]) -> None:
        self.diagnostics = tuple(diagnostics)
        super().__init__(
            f"{len(diagnostics)} compile error(s):\n" + "\n".join(f"  {d}" for d in diagnostics)
        )

    @property
    def codes(self) -> set[str]:
        return {d.code for d in self.diagnostics}


@dataclass(frozen=True)
class CompiledAgent:
    """A validated, immutable, versioned agent: the unit that is published and pinned."""

    agent: AgentDefinition
    digest: str  # content identity (excludes the version label)
    manifest: AgentManifest | None  # present when it came from a manifest (so it is storable)
    schema_version: int = MANIFEST_SCHEMA_VERSION
    compiler_version: str = COMPILER_VERSION

    @property
    def agent_id(self) -> str:
        return self.agent.agent_id

    @property
    def version(self) -> str:
        return self.agent.version


# --- public API ---
def compile_manifest(raw: AgentManifest | dict[str, Any]) -> CompiledAgent:
    diagnostics: list[Diagnostic] = []
    manifest = raw if isinstance(raw, AgentManifest) else _parse(raw, diagnostics)
    if manifest is None:
        raise CompileError(diagnostics)
    diagnostics += check_versions(manifest)
    diagnostics += check_references(manifest)
    if diagnostics:
        raise CompileError(diagnostics)
    try:
        agent = build_definition(manifest)
    except (DefinitionError, ValidationError, ValueError) as exc:
        raise CompileError([Diagnostic("DEFINITION_INVALID", "agent", _first_line(exc))]) from exc
    diagnostics += check_agent(agent)
    if diagnostics:
        raise CompileError(diagnostics)
    return CompiledAgent(agent=agent, digest=agent_digest(agent), manifest=manifest)


def compile_agent(agent: AgentDefinition) -> CompiledAgent:
    """Python-built agents get the same post-build checks (and a digest), but no manifest."""
    problems = check_agent(agent)
    try:
        Version(agent.version)
    except DefinitionError as exc:
        problems.append(Diagnostic("INVALID_VERSION", "version", str(exc)))
    if problems:
        raise CompileError(problems)
    return CompiledAgent(agent=agent, digest=agent_digest(agent), manifest=None)


# --- digest ---
def agent_document(agent: AgentDefinition) -> dict[str, Any]:
    """Canonical JSON-able content of an agent, however it was written."""
    return {
        "agent_id": agent.agent_id,
        "persona": agent.persona,
        "timezone": agent.timezone,
        "capabilities": [
            {
                "name": c.name,
                "description": c.description,
                "risk": c.risk,
                "confirmation_required": c.confirmation_required,
                "summary_template": c.summary_template,
                "input": schema_fingerprint(c.input_model),
                "output": schema_fingerprint(c.output_model),
            }
            for c in sorted(agent.capabilities, key=lambda c: c.name)
        ],
        "tools": [
            {
                **t.model_dump(mode="json", exclude={"input_model", "output_model"}),
                "input": schema_fingerprint(t.input_model),
                "output": schema_fingerprint(t.output_model) if t.output_model else None,
            }
            for t in sorted(agent.tools, key=lambda t: t.name)
        ],
        "bindings": [
            b.model_dump(mode="json", by_alias=True)
            for b in sorted(agent.bindings, key=lambda b: b.capability)
        ],
        "allowed_capabilities": sorted(agent.allowed_capabilities),
        "flows": [f.model_dump(mode="json") for f in agent.flows],
        "max_history_messages": agent.max_history_messages,
        "fallback_reply": agent.fallback_reply,
        "cancelled_after_effect_reply": agent.cancelled_after_effect_reply,
        "max_tokens_per_turn": agent.max_tokens_per_turn,
        "max_flow_depth": agent.max_flow_depth,
        "confirmation": agent.confirmation.model_dump(mode="json"),
        "confirmation_prompt_enabled": agent.confirmation_prompt_enabled,
    }


def agent_digest(agent: AgentDefinition) -> str:
    return stable_hash(agent_document(agent))


# --- stage 1: parse + versions + references (on the manifest, before anything is built) ---
def _first_line(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__


def _parse(raw: dict[str, Any], diagnostics: list[Diagnostic]) -> AgentManifest | None:
    try:
        return AgentManifest.model_validate(raw)
    except ValidationError as exc:
        for error in exc.errors():
            where = ".".join(str(p) for p in error["loc"]) or "manifest"
            diagnostics.append(Diagnostic("MANIFEST_INVALID", where, error["msg"]))
        return None
    except DefinitionError as exc:  # a rule a model enforces while parsing (e.g. flow structure)
        diagnostics.append(Diagnostic("MANIFEST_INVALID", "manifest", _first_line(exc)))
        return None


def check_versions(manifest: AgentManifest) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    if manifest.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        found.append(
            Diagnostic(
                "INCOMPATIBLE_SCHEMA_VERSION",
                "schema_version",
                f"manifest format {manifest.schema_version} is not supported by this compiler "
                f"(supports {sorted(SUPPORTED_SCHEMA_VERSIONS)})",
            )
        )
    try:
        Version(manifest.version)
    except DefinitionError as exc:
        found.append(Diagnostic("INVALID_VERSION", "version", str(exc)))
    try:
        if Version(manifest.min_framework) > Version(FRAMEWORK_VERSION):
            found.append(
                Diagnostic(
                    "INCOMPATIBLE_FRAMEWORK",
                    "min_framework",
                    f"requires framework >= {manifest.min_framework}, this is {FRAMEWORK_VERSION}",
                )
            )
    except DefinitionError as exc:
        found.append(Diagnostic("INVALID_VERSION", "min_framework", str(exc)))
    return found


def check_references(manifest: AgentManifest) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    caps = {c.name: c for c in manifest.capabilities}
    tools = {t.name: t for t in manifest.tools}
    for label, items in (("capabilities", manifest.capabilities), ("tools", manifest.tools)):
        names = [i.name for i in items]
        for name in sorted({n for n in names if names.count(n) > 1}):
            found.append(Diagnostic("DUPLICATE_NAME", label, f"{name!r} is declared twice"))
    bound: set[str] = set()
    for i, b in enumerate(manifest.bindings):
        where = f"bindings[{i}]"
        if b.capability in bound:
            found.append(Diagnostic("DUPLICATE_BINDING", where, f"{b.capability!r} bound twice"))
        bound.add(b.capability)
        if b.capability not in caps:
            found.append(
                Diagnostic("UNKNOWN_CAPABILITY", where, f"{b.capability!r} is not defined")
            )
        if b.tool not in tools:
            found.append(Diagnostic("UNKNOWN_TOOL", where, f"{b.tool!r} is not defined"))
        if b.capability in caps and b.tool in tools and b.risk is not None:
            floor = max((caps[b.capability].risk, tools[b.tool].risk), key=lambda r: RISK_ORDER[r])
            if RISK_ORDER[b.risk] < RISK_ORDER[floor]:
                found.append(
                    Diagnostic(
                        "RISK_DOWNGRADE",
                        where,
                        f"binding declares {b.risk!r} below {floor!r}: a binding can raise "
                        "protection, never lower it",
                    )
                )
    for name in manifest.allowed_capabilities:
        if name not in caps:
            found.append(Diagnostic("UNKNOWN_CAPABILITY", "allowed_capabilities", f"{name!r}"))
        elif name not in bound:
            found.append(
                Diagnostic("UNBOUND_CAPABILITY", "allowed_capabilities", f"{name!r} has no binding")
            )
    for tool in manifest.tools:
        rec = tool.recovery
        if rec is not None and rec.strategy == "status_lookup":
            if rec.lookup_capability not in caps:
                found.append(
                    Diagnostic(
                        "UNKNOWN_CAPABILITY",
                        f"tools.{tool.name}.recovery",
                        f"lookup capability {rec.lookup_capability!r} is not defined",
                    )
                )
            elif rec.lookup_capability not in bound:
                found.append(
                    Diagnostic(
                        "UNBOUND_CAPABILITY",
                        f"tools.{tool.name}.recovery",
                        f"lookup capability {rec.lookup_capability!r} has no binding",
                    )
                )
    for flow in manifest.flows:
        for step in flow.steps:
            if isinstance(step, Invoke | Propose) and step.capability not in caps:
                found.append(
                    Diagnostic(
                        "UNKNOWN_CAPABILITY",
                        f"flows.{flow.name}.{step.id}",
                        f"{step.capability!r} is not defined",
                    )
                )
    return found


# --- stage 2: checks on the built agent (shared by manifests and Python definitions) ---
def check_agent(agent: AgentDefinition) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    for binding in agent.bindings:
        resolved = agent.resolve(binding.capability)
        if resolved is None:  # pragma: no cover - AgentDefinition guarantees it
            continue
        found += _check_binding(resolved, binding)
    for cap in agent.capabilities:
        found += _check_summary(cap.name, cap.summary_template, cap.input_model)
    found += _check_flow_inputs(agent)
    return found


def _check_summary(name: str, template: str | None, model: type[BaseModel]) -> list[Diagnostic]:
    if not template:
        return []
    unknown = sorted(set(re.findall(r"{(\w+)}", template)) - set(model.model_fields))
    return [
        Diagnostic("SUMMARY_UNKNOWN_FIELD", f"capabilities.{name}.summary_template", f"{{{u}}}")
        for u in unknown
    ]


def _check_binding(resolved: ResolvedToolBinding, binding: CapabilityBinding) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    name = f"bindings.{binding.capability}"
    cap_in, cap_out = resolved.capability.input_model, resolved.capability.output_model
    tool_in, tool_out = resolved.tool.input_model, resolved.tool.output_model

    # input_map: capability input -> tool args
    found += _check_targets(f"{name}.input_map", binding.input_map, tool_in)
    found += _check_sources(f"{name}.input_map", binding.input_map, cap_in)
    # output_map: tool response -> capability output
    found += _check_targets(f"{name}.output_map", binding.output_map, cap_out)
    if tool_out is not None:
        found += _check_sources(f"{name}.output_map", binding.output_map, tool_out)

    # error_map
    for status in binding.error_map.http_status:
        if not 100 <= status <= 599:
            found.append(
                Diagnostic("ERROR_MAP_INVALID_STATUS", f"{name}.error_map", f"HTTP status {status}")
            )
    if resolved.effective_risk != "read":
        ambiguous = [
            *(("default_5xx", r) for k, r in binding.error_map.default_5xx.items() if k == "write"),
            *(("timeout", r) for k, r in binding.error_map.timeout.items() if k == "write"),
            *(
                (f"http_status[{s}]", r)
                for s, r in binding.error_map.http_status.items()
                if s >= 500
            ),
        ]
        for where, rule in ambiguous:
            if rule.type != "unknown":
                found.append(
                    Diagnostic(
                        "AMBIGUOUS_WRITE_ERROR_MAP",
                        f"{name}.error_map.{where}",
                        f"a write that may have reached the system cannot be {rule.type!r}; "
                        "map it to 'unknown' (it is reconciled, never retried blind)",
                    )
                )
    return found


def _check_targets(path: str, spec: dict[str, MapExpr], model: type[BaseModel]) -> list[Diagnostic]:
    found = [
        Diagnostic("MAPPING_UNKNOWN_TARGET", path, f"{target!r} is not a field of {model.__name__}")
        for target in spec
        if target not in model.model_fields
    ]
    required = {n for n, f in model.model_fields.items() if f.is_required()}
    found += [
        Diagnostic("MAPPING_MISSING_REQUIRED", path, f"required field {n!r} of {model.__name__}")
        for n in sorted(required - set(spec))
    ]
    for target, expr in spec.items():
        field = model.model_fields.get(target)
        if isinstance(expr, Each) and field is not None:
            item = _item_model(field.annotation)
            if item is not None:
                found += _check_targets(f"{path}.{target}", expr.map, item)
    return found


def _check_sources(path: str, spec: dict[str, MapExpr], model: type[BaseModel]) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    for target, expr in spec.items():
        found += _check_expr(f"{path}.{target}", expr, model)
    return found


def _check_expr(path: str, expr: MapExpr, model: type[BaseModel]) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    if isinstance(expr, str):
        found += _check_path(path, expr, model)
    elif isinstance(expr, Ref):
        found += _check_path(path, expr.from_, model)
        if expr.transform is not None and expr.transform not in TRANSFORM_NAMES:
            found.append(
                Diagnostic(
                    "UNKNOWN_TRANSFORM", path, f"transform {expr.transform!r} does not exist"
                )
            )
        if expr.enum is not None:
            found += _check_enum_map(path, expr, model)
    elif isinstance(expr, Each):
        found += _check_path(path, expr.from_each, model)
        item = _walk(model, expr.from_each)
        if isinstance(item, type) and issubclass(item, BaseModel):
            for target, inner in expr.map.items():
                found += _check_expr(f"{path}.{target}", inner, item)
    elif not isinstance(expr, Const):  # pragma: no cover
        found.append(Diagnostic("MAPPING_INVALID", path, f"unsupported expression {expr!r}"))
    return found


def _check_enum_map(path: str, ref: Ref, model: type[BaseModel]) -> list[Diagnostic]:
    leaf = _walk(model, ref.from_)
    values = _literal_values(leaf)
    if values is None or ref.enum is None:
        return []
    found = [
        Diagnostic("ENUM_MAPPING_UNKNOWN_KEY", path, f"{k!r} is not a value of the source field")
        for k in sorted(set(ref.enum) - values)
    ]
    found += [
        Diagnostic("ENUM_MAPPING_INCOMPLETE", path, f"source value {v!r} has no mapping")
        for v in sorted(values - set(ref.enum))
    ]
    return found


_SEGMENT = re.compile(r"\.([A-Za-z_][\w-]*)|\[(\d+|\*)\]")


def _check_path(path: str, expr: str, model: type[BaseModel]) -> list[Diagnostic]:
    """Walks `$.a.b[*].c` through the model; a key the model does not have is an error."""
    if not expr.startswith("$"):
        return [Diagnostic("MAPPING_INVALID_PATH", path, f"{expr!r} must start with '$'")]
    rest, pos, current = expr[1:], 0, model
    while pos < len(rest):
        m = _SEGMENT.match(rest, pos)
        if m is None:
            return [Diagnostic("MAPPING_INVALID_PATH", path, f"bad path syntax {expr!r}")]
        pos = m.end()
        if m.group(1) is None:
            continue  # list index: the element type is what matters, handled by _step
        nxt = _step(current, m.group(1))
        if nxt is _UNKNOWN_KEY:
            return [
                Diagnostic(
                    "MAPPING_UNKNOWN_SOURCE",
                    path,
                    f"{expr!r}: {m.group(1)!r} is not a field of {current.__name__}",
                )
            ]
        if not (isinstance(nxt, type) and issubclass(nxt, BaseModel)):
            return []  # reached a leaf/untyped value: nothing more to verify
        current = nxt
    return []


_UNKNOWN_KEY = object()


def _step(model: type[BaseModel], key: str) -> Any:
    field = model.model_fields.get(key)
    if field is None:
        return _UNKNOWN_KEY
    annotation = _unwrap_optional(field.annotation)
    item = _item_model(annotation)
    if item is not None:
        return item
    return annotation


def _walk(model: type[BaseModel], expr: str) -> Any:
    """Type at the end of a path, or None when it cannot be determined."""
    current: Any = model
    for key in re.findall(r"\.([A-Za-z_][\w-]*)", expr):
        if not (isinstance(current, type) and issubclass(current, BaseModel)):
            return None
        current = _step(current, key)
        if current is _UNKNOWN_KEY:
            return None
    return current


def _unwrap_optional(annotation: Any) -> Any:
    if get_origin(annotation) in (Union, UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _item_model(annotation: Any) -> type[BaseModel] | None:
    annotation = _unwrap_optional(annotation)
    if get_origin(annotation) is list:
        args = get_args(annotation)
        inner = _unwrap_optional(args[0]) if args else None
        if isinstance(inner, type) and issubclass(inner, BaseModel):
            return inner
    return None


def _literal_values(annotation: Any) -> set[str] | None:
    annotation = _unwrap_optional(annotation)
    if get_origin(annotation) is Literal:
        return {str(v) for v in get_args(annotation)}
    return None


def _check_flow_inputs(agent: AgentDefinition) -> list[Diagnostic]:
    """A flow's capability inputs must be the capability's real inputs, all required ones given,
    and an enum slot may only offer values the capability accepts."""
    found: list[Diagnostic] = []
    for flow in agent.flows:
        for step in flow.steps:
            if not isinstance(step, Invoke | Propose):
                continue
            cap = next((c for c in agent.capabilities if c.name == step.capability), None)
            if cap is None:
                continue
            where = f"flows.{flow.name}.{step.id}.inputs"
            fields = cap.input_model.model_fields
            found += [
                Diagnostic("FLOW_INPUT_UNKNOWN", where, f"{k!r} is not an input of {cap.name!r}")
                for k in step.inputs
                if k not in fields
            ]
            found += [
                Diagnostic("FLOW_INPUT_MISSING", where, f"required input {k!r} of {cap.name!r}")
                for k, f in fields.items()
                if f.is_required() and k not in step.inputs
            ]
            for key, expr in step.inputs.items():
                if not isinstance(expr, Slot) or key not in fields:
                    continue
                accepted = _literal_values(fields[key].annotation)
                slot = flow.slot(expr.name)
                if (
                    accepted is not None
                    and slot.type == "enum"
                    and not set(slot.choices) <= accepted
                ):
                    extra = sorted(set(slot.choices) - accepted)
                    found.append(
                        Diagnostic(
                            "FLOW_ENUM_MISMATCH",
                            where,
                            f"slot {slot.name!r} offers {extra} which {cap.name!r}.{key} rejects",
                        )
                    )
    return found
