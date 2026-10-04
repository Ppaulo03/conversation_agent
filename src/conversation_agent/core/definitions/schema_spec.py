"""Serializable schemas for manifests (Phase 5).

Python definitions describe capability/tool inputs and outputs with Pydantic classes. A manifest
(YAML/JSON) cannot hold a class, so it holds a small typed description instead, and `build_model`
turns it into the same kind of strict Pydantic model. Only what the framework actually needs is
supported; anything else is a manifest error, never silently ignored.

`schema_fingerprint` is the common ground: it canonicalises ANY Pydantic model (hand-written or
built from a spec) into comparable JSON (titles dropped, `$ref`s inlined), so two agents are
the same agent when their schemas say the same thing, whichever way they were written.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, create_model, model_validator

from conversation_agent.core.canonical import canonicalize

FieldType = Literal[
    "string", "integer", "number", "boolean", "date", "datetime", "enum", "list", "object"
]


class FieldSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: FieldType
    description: str = ""
    required: bool = True
    nullable: bool = False  # the value may be null (still present when `required`)
    values: tuple[str, ...] = ()  # enum
    items: FieldSpec | None = None  # list
    fields: dict[str, FieldSpec] = Field(default_factory=dict)  # object
    default: Any = None  # honoured only when explicitly set (like `Ref.default`)
    gt: int | float | None = None
    ge: int | float | None = None
    lt: int | float | None = None
    le: int | float | None = None
    min_length: int | None = None
    max_length: int | None = None

    @model_validator(mode="after")
    def _shape_matches_type(self) -> FieldSpec:
        if self.type == "enum" and not self.values:
            raise ValueError("an enum field needs `values`")
        if self.type != "enum" and self.values:
            raise ValueError("`values` only applies to enum fields")
        if self.type == "list" and self.items is None:
            raise ValueError("a list field needs `items`")
        if self.type != "list" and self.items is not None:
            raise ValueError("`items` only applies to list fields")
        if self.type == "object" and not self.fields:
            raise ValueError("an object field needs `fields`")
        if self.type != "object" and self.fields:
            raise ValueError("`fields` only applies to object fields")
        numeric = {"integer", "number"}
        if self.type not in numeric and any(
            v is not None for v in (self.gt, self.ge, self.lt, self.le)
        ):
            raise ValueError("gt/ge/lt/le only apply to numeric fields")
        if self.type not in {"string", "list"} and (
            self.min_length is not None or self.max_length is not None
        ):
            raise ValueError("min_length/max_length only apply to strings and lists")
        return self


FieldSpec.model_rebuild()
ModelSpec = dict[str, FieldSpec]


def _python_type(spec: FieldSpec, name: str, strict: bool) -> Any:
    base: Any
    match spec.type:
        case "string":
            base = str
        case "integer":
            base = int
        case "number":
            base = float
        case "boolean":
            base = bool
        case "date":
            base = date
        case "datetime":
            base = AwareDatetime
        case "enum":
            base = Literal[spec.values]
        case "list":
            assert spec.items is not None
            base = list[_python_type(spec.items, f"{name}Item", strict)]  # type: ignore[misc]
        case "object":
            base = build_model(name, spec.fields, strict=strict)
    return base


def _field(spec: FieldSpec) -> Any:
    constraints = {
        k: v
        for k, v in {
            "gt": spec.gt,
            "ge": spec.ge,
            "lt": spec.lt,
            "le": spec.le,
            "min_length": spec.min_length,
            "max_length": spec.max_length,
        }.items()
        if v is not None
    }
    return constraints


def build_model(name: str, fields: ModelSpec, *, strict: bool = True) -> type[BaseModel]:
    """A model equivalent to the spec. `strict` (extra=forbid) for what the framework defines;
    lenient for responses of EXTERNAL systems, which may add fields at any time."""
    definitions: dict[str, Any] = {}
    for field_name, spec in fields.items():
        annotation = _python_type(spec, f"{name}_{field_name}", strict)
        optional_without_default = not spec.required and "default" not in spec.model_fields_set
        if spec.nullable or optional_without_default:
            annotation = annotation | None
        constraints = _field(spec)
        if "default" in spec.model_fields_set:
            fallback: Any = spec.default
        else:
            fallback = ... if spec.required else None
        info = Field(
            fallback,
            description=spec.description or None,
            **constraints,
        )
        definitions[field_name] = (annotation, info)
    return create_model(
        name, __config__=ConfigDict(extra="forbid" if strict else "ignore"), **definitions
    )


# --- canonical form of any model ---


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            target = defs[str(node["$ref"]).rsplit("/", 1)[-1]]
            merged = {**_inline(target, defs), **{k: v for k, v in node.items() if k != "$ref"}}
            return merged
        return {k: _inline(v, defs) for k, v in node.items() if k not in ("title", "$defs")}
    if isinstance(node, list):
        return [_inline(v, defs) for v in node]
    return node


def schema_fingerprint(model: type[BaseModel]) -> dict[str, Any]:
    """Comparable JSON Schema: no titles/class names, references inlined, sorted keys."""
    schema = model.model_json_schema(mode="validation")
    inlined = _inline(schema, schema.get("$defs", {}))
    return cast(dict[str, Any], canonicalize(inlined))
