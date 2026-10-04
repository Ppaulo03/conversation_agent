"""AgentRegistry: immutable, versioned storage of compiled agents (Phase 5)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from conversation_agent.core.compiler import CompiledAgent


class PublishedAgent(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_id: str
    version: str
    digest: str
    published_at: datetime | None = None
    breaking_changes: tuple[str, ...] = ()
    created: bool = True  # False when the identical version was already there


class AgentRegistry(Protocol):
    async def publish(self, compiled: CompiledAgent) -> PublishedAgent:
        """Immutable: same content is a no-op, other content under the same version is an
        error, and versions only move forward (see `core.publishing`)."""
        ...

    async def get(self, agent_id: str, version: str) -> CompiledAgent | None: ...
    async def latest(self, agent_id: str) -> CompiledAgent | None: ...
    async def versions(self, agent_id: str) -> list[str]: ...
