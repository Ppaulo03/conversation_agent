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

# A Tag is a plain name ("string", "date", ...) or a structured tuple:
#   ("enum", frozenset(values))
#   ("list", element Tag)
#   ("object", ((name, Tag, required), ...) | None, forbids_extra)   (None: untyped object)
Tag = Any
ANY = "any"
NONE = ("none",)  # the type of the value null
NEVER = "never"  # the non-null part of null
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


def is_optional(annotation: Any) -> bool:
    ann = annotation
    while get_origin(ann) is Annotated:
        ann = get_args(ann)[0]
    return get_origin(ann) in (Union, UnionType) and type(None) in get_args(ann)


def tag_of_annotation(annotation: Any, depth: int = 0) -> Tag:
    ann = annotation
    while get_origin(ann) is Annotated:
        ann = get_args(ann)[0]
    if depth > 6:
        return ANY  # recursive models: stop checking, never loop
    if get_origin(ann) in (Union, UnionType):
        members = [a for a in get_args(ann) if a is not type(None)]
        inner = unify([tag_of_annotation(a, depth + 1) for a in members])
        return nullable(inner) if type(None) in get_args(ann) else inner
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
        args = get_args(ann)
        return ("list", tag_of_annotation(args[0], depth + 1) if args else ANY)
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        fields = tuple(
            (name, tag_of_annotation(f.annotation, depth + 1), f.is_required())
            for name, f in sorted(ann.model_fields.items())
        )
        return ("object", fields, ann.model_config.get("extra") == "forbid")
    return ANY


def tag_of_value(value: Any) -> Tag:
    if value is None:
        return NONE
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return ("list", unify([tag_of_value(v) for v in value]))
    if isinstance(value, dict):
        fields = tuple((str(k), tag_of_value(v), True) for k, v in sorted(value.items()))
        return ("object", fields, False)
    return ANY


# --- nullability and unions: a mix of types is a UNION, never "anything goes" ---
def split_null(tag: Tag) -> tuple[bool, Tag]:
    """(can it be null?, the non-null part). `None` itself is (True, NEVER)."""
    if tag == NONE:
        return True, NEVER
    if isinstance(tag, tuple) and tag[0] == "nullable":
        return True, split_null(tag[1])[1]
    return False, tag


def nullable(tag: Tag) -> Tag:
    if tag == ANY:
        return ANY
    base = split_null(tag)[1]
    return NONE if base == NEVER else ("nullable", base)


def unify(tags: list[Tag]) -> Tag:
    """The type of a value that may be any of `tags`: one type when they agree, a union when
    they do not (NOT `any`), nullable when any member can be null."""
    can_be_null, bases = False, set()
    for tag in tags:
        null, base = split_null(tag)
        can_be_null = can_be_null or null
        for member in base[1] if isinstance(base, tuple) and base[0] == "union" else [base]:
            if member == ANY:
                return ANY
            if member != NEVER:
                bases.add(member)
    if not bases:
        return NONE if can_be_null else ANY
    merged: Tag = next(iter(bases)) if len(bases) == 1 else ("union", frozenset(bases))
    return nullable(merged) if can_be_null else merged


def compatible(source: Tag, target: Tag) -> bool:
    if ANY in (source, target):
        return True
    source_null, source_base = split_null(source)
    target_null, target_base = split_null(target)
    if source_null and not target_null:
        return False  # a value that can be null cannot fill a field that cannot
    if source_base == NEVER or ANY in (source_base, target_base):
        return True
    return _base_compatible(source_base, target_base)


def _base_compatible(source: Tag, target: Tag) -> bool:
    if isinstance(source, tuple) and source[0] == "union":
        return all(_base_compatible(m, target) for m in source[1])
    if isinstance(target, tuple) and target[0] == "union":
        return any(_base_compatible(source, m) for m in target[1])
    if source == target:
        return True
    if isinstance(source, tuple) and isinstance(target, tuple):
        if source[0] != target[0]:
            return False
        if source[0] == "enum":
            return bool(source[1] <= target[1])
        if source[0] == "list":
            return compatible(source[1], target[1])
        return _object_compatible(source, target)
    if isinstance(source, tuple):
        return bool(source[0] == "enum" and target == "string")
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


def _object_compatible(source: Tag, target: Tag) -> bool:
    """Assignable when every required target field is provided with a compatible type (null
    included), optional ones that are provided are compatible, and a strict target gets no field
    it does not know. Keys are always present: a dumped model writes optional fields as null."""
    if source[1] is None or target[1] is None:
        return True  # untyped on either side: nothing to verify
    have = {name: tag for name, tag, _ in source[1]}
    wanted = {name: (tag, required) for name, tag, required in target[1]}
    for name, (tag, required) in wanted.items():
        if name not in have:
            if required:
                return False
            continue
        if not compatible(have[name], tag):
            return False
    return not (target[2] and set(have) - set(wanted))


def list_element(tag: Tag) -> Tag | None:
    """The element type when `tag` is a (possibly nullable) list."""
    base = split_null(tag)[1]
    return base[1] if isinstance(base, tuple) and base[0] == "list" else None


def is_list(tag: Tag) -> bool:
    return list_element(tag) is not None


def show(tag: Tag) -> str:
    if tag == NONE:
        return "null"
    if isinstance(tag, tuple):
        if tag[0] == "nullable":
            return f"{show(tag[1])}|null"
        if tag[0] == "union":
            return " | ".join(sorted(show(m) for m in tag[1]))
        if tag[0] == "enum":
            return f"enum{sorted(tag[1])}"
        if tag[0] == "list":
            return f"list[{show(tag[1])}]"
        if tag[1] is None:
            return "object"
        return "object{" + ", ".join(f"{n}: {show(t)}" for n, t, _ in tag[1]) + "}"
    return str(tag)


# --- paths ---
def resolve_path(model: type[BaseModel], expr: str) -> tuple[Any, str | None]:
    """(annotation at the end of the path | None if untyped, error message | None).

    A key the model lacks, a `.key` read on a scalar or list, and an index on a non-list are
    errors; untyped values (Any, dict) end the walk without a verdict."""
    if not expr.startswith("$"):
        return None, f"{expr!r} must start with '$'"
    rest, pos = expr[1:], 0
    fanned = False
    through_null = False  # an optional object on the way: the value may be absent (null)
    current: Any = model
    while pos < len(rest):
        m = _SEGMENT.match(rest, pos)
        if m is None:
            return None, f"bad path syntax {expr!r}"
        pos = m.end()
        through_null = through_null or is_optional(current)
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
            if m.group(2) == "*":
                fanned = True
            if get_origin(node) is list:
                args = get_args(node)
                current = args[0] if args else Any
            elif node is Any or node is list or node is dict:
                return None, None
            else:
                return None, f"{expr!r}: cannot index a {show(tag_of_annotation(node))}"
    if fanned and current is not None:
        current = list[current]  # `[*]` yields a LIST of whatever follows (runtime fan-out)
    if through_null and current is not None:
        current = current | None
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
        return ("list", ANY), []
    assert isinstance(expr, Ref)
    ann, err = resolve_path(source, expr.from_)
    if err:
        problems.append(err)
    tag: Tag = tag_of_annotation(ann) if ann is not None else ANY
    has_default = "default" in expr.model_fields_set
    is_null, base = split_null(tag)
    if (expr.enum is not None or expr.transform is not None) and is_null and not has_default:
        problems.append(
            f"the source can be null ({show(tag)}) and nothing handles it: give the mapping a "
            "`default` (a null makes enum maps and transforms fail)"
        )
    if expr.enum is not None:
        if base != ANY and base != "string" and not (isinstance(base, tuple) and base[0] == "enum"):
            problems.append(f"an enum map needs a string/enum source, got {show(base)}")
        tag = unify([tag_of_value(v) for v in expr.enum.values()])
    if expr.transform is not None and expr.transform in TRANSFORM_SPECS:
        accepted, produced = TRANSFORM_SPECS[expr.transform]
        if base != ANY and base not in accepted:
            problems.append(
                f"transform {expr.transform!r} takes {sorted(accepted)}, got {show(base)}"
            )
        tag = produced
    if has_default:
        # the default REPLACES an absent or null value: what comes out is "the mapped value,
        # or the default"
        fallback = tag_of_value(expr.default)
        tag = unify([split_null(tag)[1], fallback])
        if fallback != NONE:
            tag = unify([tag])
    return tag, problems
