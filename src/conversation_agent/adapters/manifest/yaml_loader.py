"""YAML -> manifest dict. `safe_load` only: a manifest is data, never code."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from conversation_agent.core.errors import DefinitionError

MAX_MANIFEST_BYTES = 1_000_000  # a manifest this large is a mistake or an attack


class _UniqueKeyLoader(yaml.SafeLoader):
    """Rejects duplicate mapping keys (YAML would silently keep the last one)."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                raise DefinitionError(
                    f"duplicate key {key!r} at line {key_node.start_mark.line + 1}"
                )
            seen.add(key)
        return super().construct_mapping(node, deep)


def _fix_bool_keys(node: Any) -> Any:
    """YAML 1.1 reads an unquoted `on:` / `off:` key as a boolean; a manifest never has boolean
    keys, so they are the words the author wrote (flow transitions are keyed by `on`)."""
    if isinstance(node, dict):
        return {
            ("on" if k is True else "off" if k is False else k): _fix_bool_keys(v)
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_fix_bool_keys(v) for v in node]
    return node


def parse_manifest_yaml(text: str) -> dict[str, Any]:
    if len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise DefinitionError("manifest is too large")
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise DefinitionError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise DefinitionError("a manifest must be a YAML mapping")
    return _fix_bool_keys(data)  # type: ignore[no-any-return]


def load_manifest_file(path: str | Path) -> dict[str, Any]:
    return parse_manifest_yaml(Path(path).read_text(encoding="utf-8"))
