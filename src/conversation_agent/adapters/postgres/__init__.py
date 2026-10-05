"""PostgreSQL adapters. Need the `postgres` extra: `pip install "conversation-agent[postgres]"`."""

from __future__ import annotations

try:
    import asyncpg  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover - exercised in a subprocess
    raise ImportError(
        'the PostgreSQL adapters need asyncpg: pip install "conversation-agent[postgres]"'
    ) from exc
