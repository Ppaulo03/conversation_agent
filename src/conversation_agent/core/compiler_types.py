"""Type inference for the compiler's mapping checks (Phase 5.1).

A mapping is only valid if the TYPE that comes out of its source (after any transform) can go
into its target. Types are coarse tags - enough to catch `date -> integer` before runtime, never
so strict that a legitimate coercion (`datetime` -> ISO `string`) is refused.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from types import UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel

from conversation_agent.core.definitions.mapping import TRANSFORM_SPECS, Const, Each, MapExpr, Ref

Tag = str | tuple[str, frozenset[str]]  # ("enum", values) or one of the plain names
ANY = "any"
_SEGMENT = re.compile(r"\.([A-Za-z_][\w-]*)|\[(\d+|\*)\]")


def unwrap(annotation: Any) -> Any:
    """Optional[X] -> X and Annotated[X, ...] -> X."""
    while True:
        if get_origin(annotation) is Annotated:
            annotation = get_args(annotation)[0]
            continue
        if get_origin(annotation) in (Union, UnionType):
            args = [a for a in get_args(annotation) if a is not type(None)]
            if len(args) == 1:
                annotation = args[0]
                continue
        return annotation


def tag_of_annotation(annotation: Any) -> Tag:
    ann = unwrap(annotation)
    if get_origin(ann) is Literal:
        return ("enum", frozenset(str(v) for v in get_args(ann)))
    if ann is bool:
        return "boolean"
    if ann is int:
        return "integer"
    if ann is float:
        return "number"
    if ann is str:
        return "string"
    if ann is datetime:
        return "datetime"
    if ann is date:
        return "date"
    if get_origin(ann) is list or ann is list:
        return "list"
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return "object"
    return ANY


def tag_of_value(value: Any) -> Tag:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return ANY


def unify(tags: list[Tag]) -> Tag:
    return tags[0] if tags and all(t == tags[0] for t in tags) else ANY


def compatible(source: Tag, target: Tag) -> bool:
    if ANY in (source, target) or source == target:
        return True
    if isinstance(source, tuple) and isinstance(target, tuple):
        return source[1] <= target[1]
    if isinstance(source, tuple):
        return target == "string"
    if isinstance(target, tuple):
        return False  # a free string/number is not known to be one of the allowed values
    plain = {
        ("integer", "number"),
        ("date", "string"),
        ("datetime", "string"),
        ("string", "date"),
        ("string", "datetime"),
    }
    return (source, target) in plain


def show(tag: Tag) -> str:
    return f"enum{sorted(tag[1])}" if isinstance(tag, tuple) else tag


# --- paths ---
def resolve_path(model: type[BaseModel], expr: str) -> tuple[Any, str | None]:
    """(annotation at the end of the path | None if untyped, error message | None).

    A key the model lacks, a `.key` read on a scalar or list, and an index on a non-list are
    errors; untyped values (Any, dict) end the walk without a verdict."""
    if not expr.startswith("$"):
        return None, f"{expr!r} must start with '$'"
    rest, pos = expr[1:], 0
    current: Any = model
    while pos < len(rest):
        m = _SEGMENT.match(rest, pos)
        if m is None:
            return None, f"bad path syntax {expr!r}"
        pos = m.end()
        node = unwrap(current)
        if m.group(1) is not None:
            key = m.group(1)
            if isinstance(node, type) and issubclass(node, BaseModel):
                field = node.model_fields.get(key)
                if field is None:
                    return None, f"{expr!r}: {key!r} is not a field of {node.__name__}"
                current = field.annotation
            elif node is Any or get_origin(node) is dict or node is dict:
                return None, None  # untyped: cannot verify further
            else:
                return None, f"{expr!r}: cannot read .{key} of a {show(tag_of_annotation(node))}"
        else:
            if get_origin(node) is list:
                args = get_args(node)
                current = args[0] if args else Any
            elif node is Any or node is list or node is dict:
                return None, None
            else:
                return None, f"{expr!r}: cannot index a {show(tag_of_annotation(node))}"
    return current, None


def element_model(annotation: Any) -> type[BaseModel] | None:
    """The model of each item of a list path (or of the object itself)."""
    node = unwrap(annotation)
    if get_origin(node) is list:
        args = get_args(node)
        node = unwrap(args[0]) if args else None
    return node if isinstance(node, type) and issubclass(node, BaseModel) else None


# --- expressions ---
def infer(expr: MapExpr, source: type[BaseModel]) -> tuple[Tag, list[str]]:
    """(type produced by `expr`, problems found while inferring it)."""
    problems: list[str] = []
    if isinstance(expr, str):
        ann, err = resolve_path(source, expr)
        if err:
            problems.append(err)
        return (tag_of_annotation(ann) if ann is not None else ANY), problems
    if isinstance(expr, Const):
        return tag_of_value(expr.const), []
    if isinstance(expr, Each):
        return "list", []
    assert isinstance(expr, Ref)
    ann, err = resolve_path(source, expr.from_)
    if err:
        problems.append(err)
    tag: Tag = tag_of_annotation(ann) if ann is not None else ANY
    if expr.enum is not None:
        if tag != ANY and tag != "string" and not isinstance(tag, tuple):
            problems.append(f"an enum map needs a string/enum source, got {show(tag)}")
        tag = unify([tag_of_value(v) for v in expr.enum.values()])
    if expr.transform is not None and expr.transform in TRANSFORM_SPECS:
        accepted, produced = TRANSFORM_SPECS[expr.transform]
        if tag != ANY and tag not in accepted:
            problems.append(
                f"transform {expr.transform!r} takes {sorted(accepted)}, got {show(tag)}"
            )
        tag = produced
    return tag, problems
