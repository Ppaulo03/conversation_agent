from __future__ import annotations

from typing import Protocol


class ReleaseResolver(Protocol):
    """Which published version a conversation runs on, per the agent's release state."""

    async def target(
        self,
        tenant_id: str,
        agent_id: str,
        conversation_id: str,
        pinned: str | None,
        idle: bool,
    ) -> str | None:
        """The version to use, or None when the agent has no release state (the caller then
        falls back to the latest published version)."""
        ...
