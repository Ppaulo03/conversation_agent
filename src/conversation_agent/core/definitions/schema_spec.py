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

from datetime import date, datetime
from typing import Any, Literal, cast

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    create_model,
    model_validator,
)

from conversation_agent.core.canonical import canonicalize

FieldType = Literal[
    "string", "integer", "number", "boolean", "date", "datetime", "enum", "list", "object"
]


class FieldSpec(BaseModel):
    """One field. Three independent things:

      required  the key must be present
      nullable  the value may be null
      default   what an ABSENT key becomes (checked against the spec when the manifest parses)

    `required: false` WITHOUT a default means an absent key becomes None, so null is accepted;
    `required: false` WITH a default and `nullable: false` accepts {} (-> the default) and a real
    value, but rejects an explicit null."""

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
        if "default" in self.model_fields_set:
            problem = value_problem(
                self, self.default, allow_none=self.nullable or not self.required
            )
            if problem:
                raise ValueError(f"default {self.default!r} is invalid: {problem}")
        return self


FieldSpec.model_rebuild()


def value_problem(spec: FieldSpec, value: Any, *, allow_none: bool = False) -> str | None:
    """Why `value` does not satisfy `spec` (None when it does)."""
    if value is None:
        return None if allow_none else "null is not allowed"
    kind = spec.type
    if kind == "string":
        if not isinstance(value, str):
            return "expected a string"
        return _length_problem(spec, len(value))
    if kind in ("integer", "number"):
        numeric = isinstance(value, int | float) and not isinstance(value, bool)
        if not numeric or (kind == "integer" and not isinstance(value, int)):
            return f"expected {kind}"
        return _range_problem(spec, value)
    if kind == "boolean":
        return None if isinstance(value, bool) else "expected a boolean"
    if kind in ("date", "datetime"):
        try:
            if kind == "date":
                date.fromisoformat(str(value))
            elif datetime.fromisoformat(str(value)).tzinfo is None:
                return "expected a datetime WITH a timezone offset"
        except ValueError:
            return f"expected an ISO {kind}"
        return None
    if kind == "enum":
        return None if value in spec.values else f"expected one of {list(spec.values)}"
    if kind == "list":
        if not isinstance(value, list):
            return "expected a list"
        assert spec.items is not None
        for item in value:
            if (problem := value_problem(spec.items, item)) is not None:
                return f"item: {problem}"
        return _length_problem(spec, len(value))
    if not isinstance(value, dict):
        return "expected an object"
    for name in value:
        if name not in spec.fields:
            return f"unknown field {name!r}"
    for name, inner in spec.fields.items():
        if name not in value:
            if inner.required and "default" not in inner.model_fields_set:
                return f"missing field {name!r}"
            continue
        if problem := value_problem(
            inner, value[name], allow_none=inner.nullable or not inner.required
        ):
            return f"{name}: {problem}"
    return None


def _length_problem(spec: FieldSpec, size: int) -> str | None:
    if spec.min_length is not None and size < spec.min_length:
        return f"shorter than {spec.min_length}"
    if spec.max_length is not None and size > spec.max_length:
        return f"longer than {spec.max_length}"
    return None


def _range_problem(spec: FieldSpec, value: float) -> str | None:
    checks = (
        (spec.gt, value > spec.gt if spec.gt is not None else True),
        (spec.ge, value >= spec.ge if spec.ge is not None else True),
        (spec.lt, value < spec.lt if spec.lt is not None else True),
        (spec.le, value <= spec.le if spec.le is not None else True),
    )
    for bound, ok in checks:
        if not ok:
            return f"{value} violates the declared bound {bound}"
    return None


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
            # coerce once, here: Pydantic does not validate defaults, so a date default would
            # otherwise stay a string inside the model
            fallback: Any = (
                TypeAdapter(annotation).validate_python(spec.default)
                if spec.default is not None
                else None
            )
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
        return {
            k: sorted(v) if k == "required" and isinstance(v, list) else _inline(v, defs)
            for k, v in node.items()
            if k not in ("title", "$defs")
        }  # `required` is a set: field order must not matter (JSONB does not keep it)
    if isinstance(node, list):
        return [_inline(v, defs) for v in node]
    return node


def _strip_docs(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_docs(v) for k, v in node.items() if k not in ("description", "examples")}
    if isinstance(node, list):
        return [_strip_docs(v) for v in node]
    return node


def schema_fingerprint(model: type[BaseModel], *, docs: bool = True) -> dict[str, Any]:
    """Comparable JSON Schema: no titles/class names, references inlined, sorted keys.

    `docs=False` also drops descriptions/examples: the COMPATIBILITY fingerprint, which changes
    only when names, types, nullability, enums or constraints change (rewording a description
    is not a breaking change)."""
    schema = model.model_json_schema(mode="validation")
    inlined = _inline(schema, schema.get("$defs", {}))
    if not docs:
        inlined = _strip_docs(inlined)
    return cast(dict[str, Any], canonicalize(inlined))
