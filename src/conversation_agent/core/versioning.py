"""Semantic versions for published agents (Phase 5): strict MAJOR.MINOR.PATCH, ordered."""

from __future__ import annotations

import re
from functools import total_ordering

from conversation_agent.core.errors import DefinitionError

_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

# Version of the manifest FORMAT this build understands, and of the framework itself. A
# manifest declaring a newer format, or requiring a newer framework, is refused at compile time.
MANIFEST_SCHEMA_VERSION = 1
FRAMEWORK_VERSION = "0.5.0"
COMPILER_VERSION = "1"
# Compiler versions whose published manifests this build still loads with identical meaning.
SUPPORTED_COMPILER_VERSIONS = frozenset({COMPILER_VERSION})


@total_ordering
class Version:
    __slots__ = ("major", "minor", "patch")

    def __init__(self, text: str) -> None:
        match = _SEMVER.match(text)
        if match is None:
            raise DefinitionError(f"{text!r} is not a MAJOR.MINOR.PATCH version")
        self.major, self.minor, self.patch = (int(g) for g in match.groups())

    def _key(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and self._key() == other._key()

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() < other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def __repr__(self) -> str:
        return f"Version({str(self)!r})"
