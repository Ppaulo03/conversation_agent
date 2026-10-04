from __future__ import annotations

from typing import Protocol

from conversation_agent.core.definitions.pack import PackManifest


class PackLoader(Protocol):
    def load(self, name: str, version: str) -> PackManifest:
        """The Pack with exactly this name and version. Raises `DefinitionError` when it does
        not exist or is not what it claims to be."""
        ...
