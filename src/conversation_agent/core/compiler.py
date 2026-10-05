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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel, ValidationError

from conversation_agent.core.canonical import stable_hash
from conversation_agent.core.compiler_types import (
    ANY,
    compatible,
    element_model,
    infer,
    is_list,
    list_element,
    resolve_path,
    show,
    split_null,
    tag_of_annotation,
    tag_of_value,
    unify,
    unwrap,
)
from conversation_agent.core.definitions.agent import AgentDefinition
from conversation_agent.core.definitions.binding import CapabilityBinding, ResolvedToolBinding
from conversation_agent.core.definitions.capability import RISK_ORDER, CapabilityDefinition
from conversation_agent.core.definitions.flow import (
    AddDays,
    FlowDefinition,
    Invoke,
    Lit,
    Propose,
    Slot,
    Table,
)
from conversation_agent.core.definitions.manifest import AgentManifest, build_definition
from conversation_agent.core.definitions.mapping import (
    TRANSFORM_NAMES,
    Const,
    Each,
    MapExpr,
    Ref,
)
from conversation_agent.core.definitions.schema_spec import schema_fingerprint
from conversation_agent.core.definitions.tool import ToolDefinition
from conversation_agent.core.errors import DefinitionError
from conversation_agent.core.packs import PackCatalog, expand_packs
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


_SEAL = object()


@dataclass(frozen=True)
class CompiledAgent:
    """A validated, immutable, versioned agent: the unit that is published and pinned."""

    agent: AgentDefinition
    digest: str  # content identity (excludes the version label)
    manifest: AgentManifest | None  # present when it came from a manifest (so it is storable)
    schema_version: int = MANIFEST_SCHEMA_VERSION
    compiler_version: str = COMPILER_VERSION
    _seal: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        # The runtime accepts only agents the compiler produced (INV-029): there is no way to
        # wrap a hand-built AgentDefinition and skip the checks.
        if self._seal is not _SEAL:
            raise TypeError("a CompiledAgent can only be produced by the compiler")

    @property
    def manifest_digest(self) -> str:
        """Identity of the PUBLISHED ARTIFACT (the manifest, metadata included), as opposed to
        `digest`, the identity of what the agent does. Two manifests that differ only in
        `min_framework` have the same `digest` but are different publications."""
        if self.manifest is None:
            return self.digest
        return stable_hash(self.manifest.model_dump(mode="json", by_alias=True, exclude_unset=True))

    @property
    def agent_id(self) -> str:
        return self.agent.agent_id

    @property
    def version(self) -> str:
        return self.agent.version


# --- public API ---
def compile_manifest(
    raw: AgentManifest | dict[str, Any], packs: PackCatalog | None = None
) -> CompiledAgent:
    """`packs` is the catalog the manifest's `packs` are installed from (exact name + version).
    The compiled agent is self-contained: it records what was installed in `pack_lock` and the
    runtime never sees a Pack."""
    diagnostics: list[Diagnostic] = []
    if isinstance(raw, AgentManifest) and raw.packs:
        raw = raw.model_dump(mode="json", by_alias=True, exclude_unset=True)
    if isinstance(raw, dict) and raw.get("packs"):
        raw, problems = expand_packs(raw, packs or {})
        diagnostics += [Diagnostic(p.code, p.where, p.message) for p in problems]
        if diagnostics:
            raise CompileError(diagnostics)
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
    return CompiledAgent(agent=agent, digest=agent_digest(agent), manifest=manifest, _seal=_SEAL)


def compile_agent(agent: AgentDefinition) -> CompiledAgent:
    """Python-built agents get the same post-build checks (and a digest), but no manifest."""
    problems = check_agent(agent)
    try:
        Version(agent.version)
    except DefinitionError as exc:
        problems.append(Diagnostic("INVALID_VERSION", "version", str(exc)))
    if problems:
        raise CompileError(problems)
    return CompiledAgent(agent=agent, digest=agent_digest(agent), manifest=None, _seal=_SEAL)


# One loader per compiler version that ever published a manifest. Bumping COMPILER_VERSION means
# adding the new compile function HERE and keeping the old one, so history is never reinterpreted
# by a newer compiler (the registry looks the loader up by the stored version).
COMPILERS: dict[str, Callable[[AgentManifest | dict[str, Any]], CompiledAgent]] = {
    COMPILER_VERSION: compile_manifest,
}


# --- digest ---
def _binding_document(binding: CapabilityBinding) -> dict[str, Any]:
    document = binding.model_dump(mode="json", by_alias=True)
    if binding.error_map.tool_error is None:  # only MCP bindings carry it: others keep their digest
        document["error_map"].pop("tool_error", None)
    return document


def _flow_document(flow: FlowDefinition) -> dict[str, Any]:
    document = flow.model_dump(mode="json")
    for slot in document["slots"]:
        if slot.get("question_check") == "off":  # only flows that opt in carry it: others keep
            slot.pop("question_check")  # the digest they always had
    return document


def _capability_document(c: CapabilityDefinition) -> dict[str, Any]:
    document: dict[str, Any] = {
        "name": c.name,
        "description": c.description,
        "risk": c.risk,
        "confirmation_required": c.confirmation_required,
        "summary_template": c.summary_template,
        "input": schema_fingerprint(c.input_model),
        "output": schema_fingerprint(c.output_model),
    }
    if c.executed_template is not None:  # only capabilities that set it carry it (digests stable)
        document["executed_template"] = c.executed_template
    return document


def _confirmation_document(agent: AgentDefinition) -> dict[str, Any]:
    document = agent.confirmation.model_dump(mode="json")
    if document.get("reprompt_unproven") is None:  # only agents that set it carry it: the others
        document.pop("reprompt_unproven", None)  # keep the digest they always had
    return document


def agent_document(agent: AgentDefinition) -> dict[str, Any]:
    """Canonical JSON-able content of an agent, however it was written."""
    document: dict[str, Any] = {
        "agent_id": agent.agent_id,
        "persona": agent.persona,
        "timezone": agent.timezone,
        "capabilities": [
            _capability_document(c) for c in sorted(agent.capabilities, key=lambda c: c.name)
        ],
        "tools": [
            {
                # `mcp` only appears for MCP tools: an agent without any keeps its digest
                **t.model_dump(
                    mode="json",
                    exclude={"input_model", "output_model"} | ({"mcp"} if t.mcp is None else set()),
                ),
                "input": schema_fingerprint(t.input_model),
                "output": schema_fingerprint(t.output_model) if t.output_model else None,
            }
            for t in sorted(agent.tools, key=lambda t: t.name)
        ],
        "bindings": [
            _binding_document(b) for b in sorted(agent.bindings, key=lambda b: b.capability)
        ],
        "allowed_capabilities": sorted(agent.allowed_capabilities),
        "flows": [_flow_document(f) for f in agent.flows],
        "max_history_messages": agent.max_history_messages,
        "fallback_reply": agent.fallback_reply,
        "cancelled_after_effect_reply": agent.cancelled_after_effect_reply,
        "max_tokens_per_turn": agent.max_tokens_per_turn,
        "max_flow_depth": agent.max_flow_depth,
        "confirmation": _confirmation_document(agent),
        "confirmation_prompt_enabled": agent.confirmation_prompt_enabled,
        "media": agent.media.model_dump(mode="json"),
        "max_media_bytes": agent.max_media_bytes,
    }
    if agent.human_request is not None:  # only agents that use it carry it (digests stay stable)
        document["human_request"] = agent.human_request.model_dump(mode="json")
    return document


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
    for tool in agent.tools:
        mapped = frozenset(k for b in agent.bindings if b.tool == tool.name for k in b.input_map)
        found += _check_transport(tool, mapped)
        found += _check_result_map(agent, tool)
    for cap in agent.capabilities:
        found += _check_summary(cap.name, cap.summary_template, cap.input_model)
        found += _check_executed(cap)
    found += _check_flow_inputs(agent)
    return found


_PATH_VAR = re.compile(r"\{(\w+)\}")


def _check_transport(
    tool: ToolDefinition, mapped: frozenset[str] = frozenset()
) -> list[Diagnostic]:
    """The HTTP spec must carry every tool argument that matters: one that is never sent leaves
    the external system to apply ITS default (a 2-hour request becomes 1 hour). That includes
    OPTIONAL arguments a binding can produce (`mapped`), unless declared `ignored`."""
    spec = tool.http
    if spec is None:
        return []
    where = f"tools.{tool.name}.http"
    fields = tool.input_model.model_fields
    path_vars = set(_PATH_VAR.findall(spec.path))
    found = [
        Diagnostic("HTTP_UNKNOWN_ARGUMENT", where, f"{part} {name!r} is not an input of the tool")
        for part, names in (
            ("path", path_vars),
            ("query", spec.query),
            ("body", spec.body),
            ("ignored", spec.ignored),
        )
        for name in sorted(names)
        if name not in fields
    ]
    sent = path_vars | set(spec.query) | set(spec.body)
    for name in sorted(set(spec.ignored)):
        if name in fields and fields[name].is_required():
            found.append(
                Diagnostic(
                    "HTTP_IGNORED_REQUIRED", where, f"{name!r} is required and cannot be ignored"
                )
            )
        if name in sent:
            found.append(
                Diagnostic("HTTP_IGNORED_BUT_SENT", where, f"{name!r} is both ignored and sent")
            )
    found += [
        Diagnostic(
            "MAPPED_TOOL_ARG_NOT_SENT",
            where,
            f"the binding produces {name!r} but the HTTP spec never sends it (a confirmed value "
            "would be dropped): add it to path/query/body or declare it `ignored`",
        )
        for name in sorted(mapped)
        if name in fields
        and name not in sent
        and name not in spec.ignored
        and not fields[name].is_required()
    ]
    found += [
        Diagnostic(
            "TOOL_ARG_NOT_SENT",
            where,
            f"required input {name!r} is in neither the path, the query nor the body",
        )
        for name, field in fields.items()
        if field.is_required() and name not in sent
    ]
    return found


def _check_result_map(agent: AgentDefinition, tool: ToolDefinition) -> list[Diagnostic]:
    """`recovery.result_map` turns the lookup capability's output into the original one: it is a
    binding like any other and is checked like one."""
    recovery = tool.recovery
    if recovery is None or recovery.result_map is None or recovery.lookup_capability is None:
        return []
    lookup = agent.resolve(recovery.lookup_capability)
    if lookup is None:
        return []
    found: list[Diagnostic] = []
    for binding in agent.bindings:
        if binding.tool != tool.name:
            continue
        resolved = agent.resolve(binding.capability)
        if resolved is None:
            continue
        where = f"tools.{tool.name}.recovery.result_map"
        original = resolved.capability.output_model
        found += _check_targets(where, recovery.result_map, original)
        found += _check_map(where, recovery.result_map, lookup.capability.output_model, original)
    return found


def _check_summary(name: str, template: str | None, model: type[BaseModel]) -> list[Diagnostic]:
    if not template:
        return []
    unknown = sorted(set(re.findall(r"{(\w+)}", template)) - set(model.model_fields))
    return [
        Diagnostic("SUMMARY_UNKNOWN_FIELD", f"capabilities.{name}.summary_template", f"{{{u}}}")
        for u in unknown
    ]


def _check_executed(cap: CapabilityDefinition) -> list[Diagnostic]:
    if not cap.executed_template:
        return []
    known = set(cap.input_model.model_fields) | set(cap.output_model.model_fields)
    unknown = sorted(set(re.findall(r"{(\w+)}", cap.executed_template)) - known)
    return [
        Diagnostic(
            "EXECUTED_UNKNOWN_FIELD", f"capabilities.{cap.name}.executed_template", f"{{{u}}}"
        )
        for u in unknown
    ]


def _check_binding(resolved: ResolvedToolBinding, binding: CapabilityBinding) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    name = f"bindings.{binding.capability}"
    cap_in, cap_out = resolved.capability.input_model, resolved.capability.output_model
    tool_in, tool_out = resolved.tool.input_model, resolved.tool.output_model

    # input_map: capability input -> tool args
    found += _check_targets(f"{name}.input_map", binding.input_map, tool_in)
    found += _check_map(f"{name}.input_map", binding.input_map, cap_in, tool_in)
    # output_map: tool response -> capability output
    found += _check_targets(f"{name}.output_map", binding.output_map, cap_out)
    if tool_out is not None:
        found += _check_map(f"{name}.output_map", binding.output_map, tool_out, cap_out)
    elif any(not isinstance(e, Const) for e in binding.output_map.values()):
        found.append(
            Diagnostic(
                "TOOL_HAS_NO_OUTPUT",
                f"{name}.output_map",
                f"tool {resolved.tool.name!r} declares no output, so nothing can be read from it",
            )
        )

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
            item = element_model(field.annotation)
            if item is not None:
                found += _check_targets(f"{path}.{target}", expr.map, item)
    return found


def _check_map(
    path: str, spec: dict[str, MapExpr], source: type[BaseModel], target: type[BaseModel]
) -> list[Diagnostic]:
    """Every expression must read something that exists, with the right transform, and what it
    produces must be able to go into the target field."""
    found: list[Diagnostic] = []
    for name, expr in spec.items():
        where = f"{path}.{name}"
        field = target.model_fields.get(name)
        found += _check_expr(where, expr, source, field.annotation if field else None)
    return found


def _check_expr(
    path: str, expr: MapExpr, source: type[BaseModel], target_annotation: Any
) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    if isinstance(expr, Ref):
        if expr.transform is not None and expr.transform not in TRANSFORM_NAMES:
            found.append(
                Diagnostic(
                    "UNKNOWN_TRANSFORM", path, f"transform {expr.transform!r} does not exist"
                )
            )
        if expr.enum is not None:
            found += _check_enum_map(path, expr, source)
    if isinstance(expr, Each):
        ann, err = resolve_path(source, expr.from_each)
        if err:
            found.append(Diagnostic(_path_code(err), path, err))
        item = element_model(ann) if ann is not None else None
        if ann is not None and err is None and item is None:
            source_tag = tag_of_annotation(ann)
            element = list_element(source_tag)
            element = source_tag if element is None else element
            if element != ANY:  # `map` reads fields of each item: items must be objects
                found.append(
                    Diagnostic(
                        "MAPPING_EACH_NEEDS_OBJECTS",
                        path,
                        f"from_each needs a list of objects, got {show(source_tag)}",
                    )
                )
        target_item = element_model(target_annotation) if target_annotation is not None else None
        if item is not None and target_item is not None:
            found += _check_map(path, expr.map, item, target_item)
        elif item is not None:
            for key, inner in expr.map.items():
                found += _check_expr(f"{path}.{key}", inner, item, None)
    else:
        _, problems = infer(expr, source)
        for problem in problems:
            code = _path_code(problem)
            if "takes" in problem:
                code = "MAPPING_TRANSFORM_INPUT"
            elif "enum map needs" in problem:
                code = "MAPPING_ENUM_SOURCE"
            found.append(Diagnostic(code, path, problem))
    if target_annotation is not None and not isinstance(expr, Each):
        produced, _ = infer(expr, source)
        wanted = tag_of_annotation(target_annotation)
        if not compatible(produced, wanted):
            found.append(
                Diagnostic(
                    "MAPPING_TYPE_MISMATCH",
                    path,
                    f"produces {show(produced)} but the target takes {show(wanted)}",
                )
            )
    elif target_annotation is not None:
        wanted = tag_of_annotation(target_annotation)
        element = list_element(wanted)
        if not (is_list(wanted) or wanted == ANY):
            found.append(
                Diagnostic(
                    "MAPPING_TYPE_MISMATCH", path, f"a list mapping cannot fill a {show(wanted)}"
                )
            )
        elif element is not None and element != ANY and not _is_object(element):
            found.append(
                Diagnostic(
                    "MAPPING_TYPE_MISMATCH",
                    path,
                    f"each item becomes an object but the target list holds {show(element)}",
                )
            )
    return found


def _is_object(tag: Any) -> bool:
    base = split_null(tag)[1]
    return isinstance(base, tuple) and base[0] == "object"


def _path_code(message: str) -> str:
    if "is not a field" in message:
        return "MAPPING_UNKNOWN_SOURCE"
    if "cannot read" in message or "cannot index" in message:
        return "MAPPING_SCALAR_TRAVERSAL"
    return "MAPPING_INVALID_PATH"


def _check_enum_map(path: str, ref: Ref, model: type[BaseModel]) -> list[Diagnostic]:
    ann, _ = resolve_path(model, ref.from_)
    values = _literal_values(ann)
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


def _literal_values(annotation: Any) -> set[str] | None:
    annotation = unwrap(annotation)
    if get_origin(annotation) is Literal:
        return {str(v) for v in get_args(annotation)}
    return None


_SLOT_TAGS = {"date": "date", "time": "string", "duration_minutes": "integer", "text": "string"}


def _check_flow_type(
    where: str, flow: FlowDefinition, key: str, expr: Any, annotation: Any
) -> list[Diagnostic]:
    """What a flow feeds into a capability input must be able to go into it."""
    if isinstance(expr, Slot):
        slot = flow.slot(expr.name)
        produced: Any = (
            ("enum", frozenset(slot.choices)) if slot.type == "enum" else _SLOT_TAGS[slot.type]
        )
    elif isinstance(expr, AddDays):
        produced = "date"
    elif isinstance(expr, Table):
        produced = unify([tag_of_value(v) for v in expr.mapping.values()])
    elif isinstance(expr, Lit):
        produced = tag_of_value(expr.value)
    else:  # pragma: no cover
        return []
    wanted = tag_of_annotation(annotation)
    if isinstance(produced, tuple) and isinstance(wanted, tuple):
        return []  # enum vs enum is reported precisely by FLOW_ENUM_MISMATCH
    if compatible(produced, wanted):
        return []
    return [
        Diagnostic(
            "FLOW_TYPE_MISMATCH",
            where,
            f"{key!r}: the flow provides {show(produced)} but the capability takes {show(wanted)}",
        )
    ]


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
                if key in fields:
                    found += _check_flow_type(where, flow, key, expr, fields[key].annotation)
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
