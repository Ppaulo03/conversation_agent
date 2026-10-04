"""Installing Packs into an agent manifest (DESIGN §33-34).

    agent manifest (data)  +  catalog of Packs  ->  plain manifest data, `packs` -> `pack_lock`

Installation is a pure transformation of data before the compiler parses it, which is why the
runtime never needs to know a Pack existed. What it guarantees:

  - a Pack is looked up by EXACT name and version; a missing one, a duplicate, or one that needs a
    newer framework is an error;
  - parameters are validated against what the Pack declared, then substituted into its templates;
  - a Pack never shadows or replaces something the agent (or another Pack) already defines: a name
    collision is an error, not an override;
  - a Pack never widens what the model may call: its capabilities become callable only through the
    agent's explicit `allow`;
  - what the Pack REQUIRES (a bound capability, an idempotent tool, a recovery strategy) is
    checked against the agent's own bindings and tools before anything runs (INV-040).
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from conversation_agent.core.definitions.capability import RISK_ORDER
from conversation_agent.core.definitions.pack import (
    KEYS,
    BindingRequirement,
    PackLock,
    PackManifest,
    PackUse,
    ParameterSpec,
)
from conversation_agent.core.definitions.schema_spec import FieldSpec, value_problem
from conversation_agent.core.versioning import FRAMEWORK_VERSION, Version

_MAP_KEY = re.compile(r"^[a-z][a-z0-9_]*$")

PackCatalog = Mapping[tuple[str, str], PackManifest]


@dataclass(frozen=True)
class PackProblem:
    code: str
    where: str
    message: str


def expand_packs(
    raw: dict[str, Any], catalog: PackCatalog
) -> tuple[dict[str, Any], list[PackProblem]]:
    """The manifest with every declared Pack installed (or the problems that prevent it)."""
    declared = raw.get("packs")
    if not declared:
        return raw, []
    problems: list[PackProblem] = []
    uses: list[PackUse] = []
    for index, item in enumerate(declared if isinstance(declared, list) else []):
        try:
            uses.append(PackUse.model_validate(item))
        except ValidationError as exc:
            problems.append(PackProblem("PACK_USE_INVALID", f"packs[{index}]", _first(exc)))
    if not isinstance(declared, list):
        problems.append(PackProblem("PACK_USE_INVALID", "packs", "`packs` must be a list"))
    if problems:
        return raw, problems

    manifest = copy.deepcopy({k: v for k, v in raw.items() if k != "packs"})
    capabilities = list(_list(manifest, "capabilities"))
    flows = list(_list(manifest, "flows"))
    allowed = list(_list(manifest, "allowed_capabilities"))
    persona = str(manifest.get("persona", ""))
    taken_capabilities = {c.get("name") for c in capabilities if isinstance(c, dict)}
    taken_flows = {f.get("name") for f in flows if isinstance(f, dict)}
    locks: list[PackLock] = []
    installed: list[tuple[PackUse, PackManifest]] = []
    seen: set[str] = set()

    for use in uses:
        where = f"packs.{use.name}"
        pack = catalog.get((use.name, use.version))
        if pack is None:
            problems.append(
                PackProblem(
                    "PACK_NOT_FOUND", where, f"pack {use.name!r} version {use.version!r} is unknown"
                )
            )
            continue
        if use.name in seen:
            problems.append(
                PackProblem("PACK_DUPLICATE", where, "the same Pack is installed twice")
            )
            continue
        seen.add(use.name)
        if _needs_newer_framework(pack):
            problems.append(
                PackProblem(
                    "PACK_INCOMPATIBLE_FRAMEWORK",
                    where,
                    f"requires framework >= {pack.min_framework}, this is {FRAMEWORK_VERSION}",
                )
            )
            continue
        values, found = _resolve_parameters(pack, use, where)
        problems += found
        if found:
            continue
        template_problems: list[PackProblem] = []
        pack_capabilities = _substitute_all(pack.capabilities, values, where, template_problems)
        pack_flows = _substitute_all(pack.flows, values, where, template_problems)
        problems += template_problems
        if template_problems:
            continue

        offered = {c.get("name") for c in pack_capabilities}
        for name in use.allow:
            if name not in offered:
                problems.append(
                    PackProblem(
                        "PACK_ALLOW_UNKNOWN",
                        where,
                        f"allow names {name!r}, not one of its capabilities",
                    )
                )
        for kind, items, names in (
            ("capability", pack_capabilities, taken_capabilities),
            ("flow", pack_flows, taken_flows),
        ):
            for item in items:
                if item.get("name") in names:
                    problems.append(
                        PackProblem(
                            "PACK_NAME_COLLISION",
                            where,
                            f"{kind} {item.get('name')!r} is already defined and a Pack never "
                            "shadows or overrides a definition",
                        )
                    )
                names.add(item.get("name"))
        capabilities += pack_capabilities
        flows += pack_flows
        allowed += [n for n in use.allow if n not in allowed]
        for fragment in pack.persona_fragments:
            persona += f"\n\n## {pack.name}: {fragment.name}\n{fragment.text}"
        locks.append(PackLock(name=pack.name, version=pack.version, digest=pack.digest))
        installed.append((use, pack))

    manifest["capabilities"] = capabilities
    manifest["flows"] = flows
    manifest["allowed_capabilities"] = allowed
    manifest["persona"] = persona
    manifest["pack_lock"] = [lock.model_dump(mode="json") for lock in locks]
    for use, pack in installed:
        problems += check_requirements(manifest, pack, f"packs.{use.name}")
    return manifest, problems


# ---------------------------------------------------------------------- parameters


def _resolve_parameters(
    pack: PackManifest, use: PackUse, where: str
) -> tuple[dict[str, Any], list[PackProblem]]:
    problems: list[PackProblem] = []
    for name in sorted(set(use.parameters) - set(pack.parameters)):
        problems.append(
            PackProblem(
                "PACK_PARAMETER_UNKNOWN", f"{where}.{name}", "the Pack declares no such parameter"
            )
        )
    values: dict[str, Any] = {}
    for name, spec in pack.parameters.items():
        if name not in use.parameters:
            if spec.required:
                problems.append(
                    PackProblem(
                        "PACK_PARAMETER_MISSING",
                        f"{where}.{name}",
                        "a required parameter is missing",
                    )
                )
            continue
        problem = _parameter_problem(spec, use.parameters[name])
        if problem:
            problems.append(PackProblem("PACK_PARAMETER_INVALID", f"{where}.{name}", problem))
        else:
            values[name] = use.parameters[name]
    return values, problems


def _parameter_problem(spec: ParameterSpec, value: Any) -> str | None:
    if spec.value is not None:
        return value_problem(spec.value, value)
    assert spec.map_of is not None
    if not isinstance(value, dict) or not value:
        return "expected a non-empty map"
    entry = FieldSpec(type="object", fields=spec.map_of)
    for key, item in value.items():
        if not isinstance(key, str) or not _MAP_KEY.fullmatch(key):
            return f"key {key!r} must be lower_snake_case"
        if (problem := value_problem(entry, item)) is not None:
            return f"{key}: {problem}"
    return None


def _substitute_all(
    templates: tuple[dict[str, Any], ...],
    values: dict[str, Any],
    where: str,
    problems: list[PackProblem],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for template in templates:
        substituted = _substitute(template, values, where, problems)
        if isinstance(substituted, dict):
            out.append(substituted)
    return out


def _substitute(node: Any, values: dict[str, Any], where: str, problems: list[PackProblem]) -> Any:
    if isinstance(node, dict):
        if "$param" in node:
            name, project = node["$param"], node.get("project")
            if name not in values:
                problems.append(
                    PackProblem(
                        "PACK_PARAMETER_MISSING", where, f"template needs {name!r}, not provided"
                    )
                )
                return None
            value = values[name]
            if project is None:
                return copy.deepcopy(value)
            if project == KEYS:
                return list(value)
            return {key: copy.deepcopy(item[project]) for key, item in value.items()}
        return {k: _substitute(v, values, where, problems) for k, v in node.items()}
    if isinstance(node, list | tuple):
        items: list[Any] = []
        for element in node:
            if isinstance(element, dict) and element.get("splice") is True and "$param" in element:
                spliced = _substitute(
                    {k: v for k, v in element.items() if k != "splice"}, values, where, problems
                )
                items += spliced if isinstance(spliced, list) else []  # a list parameter, in place
            else:
                items.append(_substitute(element, values, where, problems))
        return items
    return node


# ---------------------------------------------------------------------- requirements


def check_requirements(
    manifest: dict[str, Any], pack: PackManifest, where: str
) -> list[PackProblem]:
    """The Pack's requirements against the agent's own bindings and tools (raw manifest data)."""
    bindings = {b.get("capability"): b for b in _list(manifest, "bindings") if isinstance(b, dict)}
    tools = {t.get("name"): t for t in _list(manifest, "tools") if isinstance(t, dict)}
    contracts = {c.get("name"): c for c in _list(manifest, "capabilities") if isinstance(c, dict)}
    problems: list[PackProblem] = []
    for requirement in pack.requires:
        problems += _requirement_problems(requirement, bindings, tools, contracts, where)
    return problems


def _requirement_problems(
    requirement: BindingRequirement,
    bindings: dict[Any, dict[str, Any]],
    tools: dict[Any, dict[str, Any]],
    contracts: dict[Any, dict[str, Any]],
    where: str,
) -> list[PackProblem]:
    here = f"{where}.requires.{requirement.capability}"

    def unmet(message: str) -> list[PackProblem]:
        return [PackProblem("PACK_REQUIREMENT_UNMET", here, message)]

    binding = bindings.get(requirement.capability)
    if binding is None:
        return unmet("the agent has no binding for this capability")
    tool = tools.get(binding.get("tool"))
    if tool is None:
        return unmet(f"its binding points to an unknown tool {binding.get('tool')!r}")
    if requirement.idempotent and tool.get("idempotency_supported") is not True:
        return unmet("the bound tool must support idempotency")
    if not (requirement.recovery or requirement.lookup_capability):
        return []
    recovery = tool.get("recovery") or {}
    strategy = recovery.get("strategy") if isinstance(recovery, dict) else None
    if strategy is None:
        risks = [
            contracts.get(requirement.capability, {}).get("risk", "read"),
            tool.get("risk", "read"),
            binding.get("risk") or "read",
        ]
        effective = max((r for r in risks if r in RISK_ORDER), key=lambda r: RISK_ORDER[r])
        strategy = "safe_retry" if effective == "read" else "human_handoff"
    if requirement.recovery and strategy not in requirement.recovery:
        needed = list(requirement.recovery)
        return unmet(f"the bound tool recovers by {strategy!r}, the Pack needs one of {needed}")
    if requirement.lookup_capability is not None and (
        not isinstance(recovery, dict)
        or recovery.get("lookup_capability") != requirement.lookup_capability
    ):
        return unmet(f"the bound tool must look up through {requirement.lookup_capability!r}")
    return []


# ---------------------------------------------------------------------- helpers


def _needs_newer_framework(pack: PackManifest) -> bool:
    try:
        return Version(pack.min_framework) > Version(FRAMEWORK_VERSION)
    except Exception:
        return True


def _list(manifest: dict[str, Any], key: str) -> list[Any]:
    value = manifest.get(key)
    return value if isinstance(value, list) else []


def _first(exc: ValidationError) -> str:
    error = exc.errors()[0]
    return f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}"
