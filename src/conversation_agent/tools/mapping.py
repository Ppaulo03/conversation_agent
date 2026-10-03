"""Applies the restricted mapping language (core.definitions.mapping) to JSON data."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from conversation_agent.core.canonical import canonicalize
from conversation_agent.core.definitions.mapping import Const, Each, MapExpr, Ref
from conversation_agent.core.errors import MappingError

_SEGMENT = re.compile(r"\.([A-Za-z_][\w-]*)|\[(\d+|\*)\]")

TRANSFORMS: dict[str, Callable[[Any], Any]] = {
    "minutes_to_hours": lambda v: v / 60,
    "hours_to_minutes": lambda v: round(v * 60),
    "datetime_to_utc": lambda v: canonicalize(datetime.fromisoformat(v)),
}

_MISSING = object()


def _select(data: Any, path: str) -> Any:
    """Tiny JSONPath subset: `$`, `.key`, `[n]`, `[*]`. Returns `_MISSING` if absent."""
    if not path.startswith("$"):
        raise MappingError(f"mapping path must start with '$': {path!r}")
    rest = path[1:]
    nodes: list[Any] = [data]
    fanned_out = False
    pos = 0
    while pos < len(rest):
        m = _SEGMENT.match(rest, pos)
        if m is None:
            raise MappingError(f"invalid mapping path syntax: {path!r}")
        pos = m.end()
        key, index = m.group(1), m.group(2)
        next_nodes: list[Any] = []
        for node in nodes:
            if key is not None:
                if isinstance(node, Mapping) and key in node:
                    next_nodes.append(node[key])
                else:
                    return _MISSING
            elif index == "*":
                if not isinstance(node, list):
                    return _MISSING
                next_nodes.extend(node)
                fanned_out = True
            else:
                if not isinstance(node, list) or int(index) >= len(node):
                    return _MISSING
                next_nodes.append(node[int(index)])
        nodes = next_nodes
    return nodes if fanned_out else nodes[0]


def _eval(expr: MapExpr, data: Any, transforms: Mapping[str, Callable[[Any], Any]]) -> Any:
    if isinstance(expr, str):
        value = _select(data, expr)
        if value is _MISSING:
            raise MappingError(f"path not found: {expr!r}")
        return value
    if isinstance(expr, Const):
        return expr.const
    if isinstance(expr, Ref):
        value = _select(data, expr.from_)
        if value is _MISSING:
            if "default" in expr.model_fields_set:
                return expr.default
            raise MappingError(f"path not found: {expr.from_!r}")
        if expr.enum is not None:
            if not isinstance(value, str) or value not in expr.enum:
                raise MappingError(f"value {value!r} not in enum mapping for {expr.from_!r}")
            value = expr.enum[value]
        if expr.transform is not None:
            fn = transforms.get(expr.transform)
            if fn is None:
                raise MappingError(f"unknown transform {expr.transform!r}")
            try:
                value = fn(value)
            except (TypeError, ValueError) as exc:
                raise MappingError(f"transform {expr.transform!r} failed: {exc}") from exc
        return value
    if isinstance(expr, Each):
        items = _select(data, expr.from_each)
        if items is _MISSING or not isinstance(items, list):
            raise MappingError(f"list path not found or not a list: {expr.from_each!r}")
        return [apply_mapping(expr.map, item, transforms) for item in items]
    raise MappingError(f"unsupported mapping expression: {expr!r}")  # pragma: no cover


def apply_mapping(
    spec: Mapping[str, MapExpr],
    data: Any,
    transforms: Mapping[str, Callable[[Any], Any]] = TRANSFORMS,
) -> dict[str, Any]:
    return {target: _eval(expr, data, transforms) for target, expr in spec.items()}
