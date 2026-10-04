"""JSON Schema (as published by an MCP server or an OpenAPI document) -> the framework's schema DSL.

Whatever a remote system publishes is UNTRUSTED input to this function. It converts a deliberately
small subset and refuses the rest, because the failure mode of a guess is silent: a keyword that is
dropped makes our model accept (or send) something the remote contract forbids, a type that is
widened lets the model invent values. So:

  - a keyword this converter does not model is an error naming it, never ignored;
  - the result is the SAME `ModelSpec` a hand-written manifest uses, so it goes through the same
    strict model builder and the same compiler checks;
  - descriptions are cut and stripped of control characters (they are prose from a stranger).

`schema_digest` is the identity used to PIN a remote schema: it changes when names, types,
nullability, enums or constraints change, not when a description is reworded.
"""

from __future__ import annotations

import re
from typing import Any

from conversation_agent.core.canonical import stable_hash
from conversation_agent.core.definitions.schema_spec import FieldSpec, ModelSpec

MAX_DEPTH = 6
MAX_FIELDS = 64
MAX_DESCRIPTION_CHARS = 300
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_DOC_KEYS = frozenset({"description", "title", "examples", "$comment", "example", "deprecated"})
# Keywords that carry no constraint on values: safe to read past.
_ANNOTATIONS = frozenset({"$schema", "$id", "additionalProperties", "readOnly", "writeOnly"})


class UnsupportedSchemaError(ValueError):
    """The remote schema uses something this converter does not model (one message per problem)."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def clean_text(value: object, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    """Prose from a remote system: bounded, single-line, no control characters."""
    text = _CONTROL.sub(" ", value if isinstance(value, str) else "")
    return " ".join(text.split())[:limit]


def _strip_docs(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_docs(v) for k, v in node.items() if k not in _DOC_KEYS}
    if isinstance(node, list):
        return [_strip_docs(v) for v in node]
    return node


def schema_digest(schema: dict[str, Any]) -> str:
    """Pin of a remote input schema (documentation excluded)."""
    return stable_hash(_strip_docs(schema))


class _Converter:
    def __init__(self, root: dict[str, Any]) -> None:
        self.root = root
        self.problems: list[str] = []
        self._seen: list[str] = []

    def bad(self, where: str, message: str) -> None:
        self.problems.append(f"{where}: {message}")

    def resolve(self, node: dict[str, Any], where: str) -> dict[str, Any] | None:
        ref = node.get("$ref")
        if ref is None:
            return node
        if not isinstance(ref, str) or not (ref == "#" or ref.startswith("#/")):
            self.bad(where, f"only local $ref is supported, got {ref!r}")
            return None
        if ref in self._seen:
            self.bad(where, f"recursive $ref {ref!r}")
            return None
        target: Any = self.root
        for part in ref[2:].split("/") if ref != "#" else []:
            target = target.get(part) if isinstance(target, dict) else None
        if not isinstance(target, dict):
            self.bad(where, f"$ref {ref!r} does not resolve")
            return None
        extra = {k: v for k, v in node.items() if k != "$ref" and k not in _DOC_KEYS}
        if extra:
            self.bad(where, f"keywords next to a $ref are not supported: {sorted(extra)}")
            return None
        self._seen.append(ref)
        return target

    def release(self, node: dict[str, Any]) -> None:
        if "$ref" in node and self._seen:
            self._seen.pop()

    def fields(self, schema: dict[str, Any], where: str, depth: int) -> ModelSpec:
        if depth > MAX_DEPTH:
            self.bad(where, f"nested deeper than {MAX_DEPTH} levels")
            return {}
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not properties:
            self.bad(where, "an object needs `properties` (free-form objects are not supported)")
            return {}
        if len(properties) > MAX_FIELDS:
            self.bad(where, f"more than {MAX_FIELDS} properties")
            return {}
        required = schema.get("required", [])
        if not isinstance(required, list):
            self.bad(where, "`required` must be a list")
            required = []
        unknown_required = sorted(str(r) for r in required if r not in properties)
        if unknown_required:
            self.bad(where, f"`required` names unknown properties {unknown_required}")
        out: ModelSpec = {}
        for name, sub in properties.items():
            if not isinstance(name, str) or not name.isidentifier():
                self.bad(where, f"property name {name!r} is not a valid identifier")
                continue
            if not isinstance(sub, dict):
                self.bad(f"{where}.{name}", "a property must be a schema object")
                continue
            spec = self.field(sub, f"{where}.{name}", depth, name in required)
            if spec is not None:
                out[name] = spec
        return out

    def field(
        self, node: dict[str, Any], where: str, depth: int, required: bool
    ) -> FieldSpec | None:
        target = self.resolve(node, where)
        if target is None:
            return None
        try:
            return self._field(target, where, depth, required)
        finally:
            self.release(node)

    def _field(
        self, node: dict[str, Any], where: str, depth: int, required: bool
    ) -> FieldSpec | None:
        nullable = False
        schema = dict(node)
        if "anyOf" in schema:
            options = schema.pop("anyOf")
            real = (
                [o for o in options if o != {"type": "null"}] if isinstance(options, list) else []
            )
            if not isinstance(options, list) or len(real) != 1 or len(options) != 2:
                self.bad(where, "only `anyOf: [X, null]` is supported")
                return None
            nullable = True
            inner = real[0]
            if not isinstance(inner, dict):
                self.bad(where, "`anyOf` options must be schema objects")
                return None
            resolved = self.resolve(inner, where)
            if resolved is None:
                return None
            schema = {**schema, **resolved}
            self.release(inner)
        kind = schema.get("type")
        if isinstance(kind, list):
            non_null = [k for k in kind if k != "null"]
            if len(non_null) != 1 or len(kind) != 2:
                self.bad(where, f"unsupported union type {kind}")
                return None
            nullable, kind = True, non_null[0]
        for keyword in schema:
            if keyword not in _SUPPORTED and keyword not in _DOC_KEYS | _ANNOTATIONS:
                self.bad(where, f"unsupported keyword {keyword!r}")
                return None
        common: dict[str, Any] = {
            "required": required,
            "nullable": nullable,
            "description": clean_text(schema.get("description")),
        }
        if "default" in schema:
            common["default"] = schema["default"]
        return self._typed(kind, schema, where, depth, common)

    def _typed(
        self, kind: object, schema: dict[str, Any], where: str, depth: int, common: dict[str, Any]
    ) -> FieldSpec | None:
        try:
            if "enum" in schema:
                values = schema["enum"]
                if kind not in (None, "string") or not (
                    isinstance(values, list) and values and all(isinstance(v, str) for v in values)
                ):
                    self.bad(where, "only non-empty enums of strings are supported")
                    return None
                return FieldSpec(type="enum", values=tuple(values), **common)
            if kind == "string":
                fmt = schema.get("format")
                bounds = _bounds(schema, "minLength", "maxLength")
                if fmt in ("date", "date-time"):
                    return FieldSpec(type="date" if fmt == "date" else "datetime", **common)
                return FieldSpec(type="string", **bounds, **common)
            if kind in ("integer", "number"):
                return FieldSpec(type=kind, **_numeric(schema), **common)
            if kind == "boolean":
                return FieldSpec(type="boolean", **common)
            if kind == "array":
                items = schema.get("items")
                if not isinstance(items, dict):
                    self.bad(where, "an array needs an `items` schema")
                    return None
                inner = self.field(items, f"{where}[]", depth + 1, True)
                if inner is None:
                    return None
                return FieldSpec(
                    type="list", items=inner, **_bounds(schema, "minItems", "maxItems"), **common
                )
            if kind == "object":
                nested = self.fields(schema, where, depth + 1)
                return FieldSpec(type="object", fields=nested, **common) if nested else None
        except ValueError as exc:  # a default that does not fit, an impossible shape...
            self.bad(where, " ".join(str(exc).split())[:200])
            return None
        self.bad(where, f"unsupported type {kind!r}")
        return None


_SUPPORTED = frozenset(
    {
        "type", "enum", "format", "default", "properties", "required", "items", "$ref",
        "minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "minItems", "maxItems",
    }
)  # fmt: skip


def _bounds(schema: dict[str, Any], low: str, high: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, name in ((low, "min_length"), (high, "max_length")):
        value = schema.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[name] = value
        elif value is not None:
            raise ValueError(f"{key} must be a non-negative integer")
    return out


def _numeric(schema: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, name in (
        ("minimum", "ge"),
        ("maximum", "le"),
        ("exclusiveMinimum", "gt"),
        ("exclusiveMaximum", "lt"),
    ):
        value = schema.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            out[name] = value
        elif value is not None:
            raise ValueError(f"{key} must be a number")
    return out


def model_spec_from_json_schema(
    schema: dict[str, Any], *, root: dict[str, Any] | None = None
) -> ModelSpec:
    """The root must be an object schema. Raises `UnsupportedSchemaError` listing EVERYTHING that
    is not supported, so one attempt tells the operator the whole story. `root` is the document
    local `$ref`s point into (an OpenAPI file); by default, the schema itself."""
    converter = _Converter(root if root is not None else schema)
    if "$ref" in schema:
        resolved = converter.resolve(schema, "root")
        if resolved is None:
            raise UnsupportedSchemaError(converter.problems)
        schema = resolved
    if schema.get("type") != "object":
        raise UnsupportedSchemaError(["root: the input schema must be an object"])
    for keyword in schema:
        if keyword not in _SUPPORTED | _DOC_KEYS | _ANNOTATIONS | {"$defs", "definitions"}:
            converter.bad("root", f"unsupported keyword {keyword!r}")
    spec = converter.fields(schema, "root", 0)
    if converter.problems:
        raise UnsupportedSchemaError(converter.problems)
    return spec
