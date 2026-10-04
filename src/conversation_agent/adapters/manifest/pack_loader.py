"""Packs from a directory: `<root>/<name>/pack.yaml` (data only: `safe_load`, never code)."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from conversation_agent.adapters.manifest.yaml_loader import parse_manifest_yaml
from conversation_agent.core.definitions.pack import PackManifest
from conversation_agent.core.errors import DefinitionError
from conversation_agent.core.packs import PackCatalog


def load_pack_file(path: str | Path) -> PackManifest:
    try:
        raw = parse_manifest_yaml(Path(path).read_text(encoding="utf-8"))
        return PackManifest.model_validate(raw)
    except ValidationError as exc:
        error = exc.errors()[0]
        where = ".".join(str(p) for p in error["loc"])
        raise DefinitionError(f"{path}: invalid pack: {where}: {error['msg']}") from exc


class DirectoryPackLoader:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()

    def load(self, name: str, version: str) -> PackManifest:
        folder = (self._root / name).resolve()
        if folder.parent != self._root:  # `name` cannot walk out of the packs directory
            raise DefinitionError(f"{name!r} is not a pack name")
        path = folder / "pack.yaml"
        if not path.is_file():
            raise DefinitionError(f"pack {name!r} not found in {self._root}")
        pack = load_pack_file(path)
        if (pack.name, pack.version) != (name, version):
            raise DefinitionError(
                f"{path} is {pack.name!r} {pack.version!r}, not {name!r} {version!r}"
            )
        return pack

    def catalog_for(self, uses: Iterable[Any]) -> PackCatalog:
        """The catalog a manifest's `packs` need, loaded once: what `compile_manifest` takes.
        A Pack that cannot be loaded is simply absent (the compiler reports PACK_NOT_FOUND)."""
        catalog: dict[tuple[str, str], PackManifest] = {}
        for use in uses:
            if not isinstance(use, dict):
                continue
            name, version = str(use.get("name")), str(use.get("version"))
            try:
                catalog[(name, version)] = self.load(name, version)
            except DefinitionError:
                continue
        return catalog
