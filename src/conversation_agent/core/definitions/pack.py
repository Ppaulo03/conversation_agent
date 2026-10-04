"""Packs: an optional distribution unit for reusable agent behavior (DESIGN §1.3, §33).

A Pack contributes DEFINITIONS (capability contracts, flows, prompt fragments, evals) and states
what it REQUIRES from the agent that installs it (bindings with certain guarantees). It cannot
carry tools, connections, bindings or secrets: those are how an agent reaches a concrete system,
and a Pack "does not implement an external API, owns no business state and gets no privileged
access". The model forbids unknown fields, so there is no place to smuggle them in.

Content is a TEMPLATE until installed: values the installing agent chooses (a service catalog, a
limit) are declared as `parameters` and referenced with `{"$param": name, "project": field}`
nodes. Installation (`core.packs.expand_packs`) validates the parameters, substitutes them, and
yields plain manifest data; after compilation nothing about the agent depends on the Pack at
runtime, and its provenance is recorded in `pack_lock`.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from conversation_agent.core.canonical import stable_hash
from conversation_agent.core.definitions.evals import EvalScenario, EvalTurn
from conversation_agent.core.definitions.schema_spec import FieldSpec

__all__ = [
    "KEYS",
    "BindingRequirement",
    "EvalScenario",
    "EvalTurn",
    "PackLock",
    "PackManifest",
    "PackUse",
    "ParameterSpec",
    "PromptFragment",
    "param_references",
]
PACK_SCHEMA_VERSION = 1
_NAME = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_PARAM_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
KEYS = "$keys"  # projection of a map parameter onto its keys


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class ParameterSpec(_Strict):
    """A value the installing agent supplies. Exactly one shape:

    value   a single value described by a FieldSpec
    map_of  a non-empty map `key -> object`, every object shaped by these fields (a catalog)
    """

    description: str = ""
    value: FieldSpec | None = None
    map_of: dict[str, FieldSpec] | None = None
    required: bool = True

    @model_validator(mode="after")
    def _one_shape(self) -> ParameterSpec:
        if (self.value is None) == (self.map_of is None):
            raise ValueError("a parameter declares exactly one of `value` and `map_of`")
        if self.map_of is not None and not self.map_of:
            raise ValueError("`map_of` needs at least one field")
        return self


class BindingRequirement(_Strict):
    """What the installing agent must provide for one capability of the Pack."""

    capability: str
    idempotent: bool = False  # the bound tool must support idempotency (a write that can retry)
    # The bound tool's effective recovery strategy must be one of these (empty: any).
    recovery: tuple[
        Literal["safe_retry", "retry_same_key", "status_lookup", "human_handoff"], ...
    ] = ()
    lookup_capability: str | None = None  # with `status_lookup`: it must look up through this one


class PromptFragment(_Strict):
    name: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=8000)


class PackManifest(_Strict):
    schema_version: int = PACK_SCHEMA_VERSION
    min_framework: str = "0.1.0"
    name: str
    version: str
    description: str = ""
    parameters: dict[str, ParameterSpec] = Field(default_factory=dict)
    # Templates: parsed as agent definitions only AFTER the parameters are substituted.
    capabilities: tuple[dict[str, Any], ...] = ()
    flows: tuple[dict[str, Any], ...] = ()
    persona_fragments: tuple[PromptFragment, ...] = ()
    requires: tuple[BindingRequirement, ...] = ()
    evals: tuple[EvalScenario, ...] = ()
    # Words the scenarios use as `{name}` that only the installing agent knows (what to call its
    # services...). A runner must be given every one of them.
    eval_variables: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError(f"pack name {value!r} must be lower-kebab-case")
        return value

    @field_validator("parameters")
    @classmethod
    def _valid_parameter_names(cls, value: dict[str, ParameterSpec]) -> dict[str, ParameterSpec]:
        for name in value:
            if not _PARAM_NAME.fullmatch(name):
                raise ValueError(f"parameter name {name!r} must be lower_snake_case")
        return value

    @model_validator(mode="after")
    def _templates_reference_declared_parameters(self) -> PackManifest:
        for where, node in (("capabilities", self.capabilities), ("flows", self.flows)):
            for reference in param_references(node):
                problem = self._reference_problem(reference)
                if problem:
                    raise ValueError(f"{where}: {problem}")
        return self

    def _reference_problem(self, reference: dict[str, Any]) -> str | None:
        extra = set(reference) - {"$param", "project", "splice"}
        if extra:
            return f"a $param node only takes `project` and `splice`, found {sorted(extra)}"
        name, project = reference["$param"], reference.get("project")
        spec = self.parameters.get(name) if isinstance(name, str) else None
        if spec is None:
            return f"$param {name!r} is not a declared parameter"
        if project is None:
            return None
        if spec.map_of is None:
            return f"$param {name!r} is a single value and cannot be projected"
        if project != KEYS and project not in spec.map_of:
            return f"$param {name!r} has no field {project!r} to project"
        return None

    @property
    def digest(self) -> str:
        return stable_hash(self.model_dump(mode="json"))


class PackUse(_Strict):
    """An agent installing a Pack: exactly which, with which parameters, and what the Pack's
    capabilities may do for THIS agent. `allow` is the agent's decision: a Pack never widens what
    the model can call by itself."""

    name: str
    version: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    allow: tuple[str, ...] = ()


class PackLock(_Strict):
    """Provenance of an installed Pack (what exactly was expanded into the agent)."""

    name: str
    version: str
    digest: str


def param_references(node: Any) -> list[dict[str, Any]]:
    """Every `{"$param": ...}` node under `node`."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if "$param" in node:
            found.append(node)
        else:
            for value in node.values():
                found += param_references(value)
    elif isinstance(node, list | tuple):
        for value in node:
            found += param_references(value)
    return found
