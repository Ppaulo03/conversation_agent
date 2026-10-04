"""AgentRegistry: immutable, versioned storage of compiled agents (Phase 5)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from conversation_agent.core.compiler import CompiledAgent


class PublishedAgent(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str
    agent_id: str
    version: str
    digest: str
    published_at: datetime | None = None
    breaking_changes: tuple[str, ...] = ()
    created: bool = True  # False when the identical version was already there


class AgentRegistry(Protocol):
    """Tenant-scoped: an agent id is only unique WITHIN a tenant (the registry is not a global
    namespace), and one tenant can never read or shadow another tenant's agents."""

    async def publish(self, tenant_id: str, compiled: CompiledAgent) -> PublishedAgent:
        """Immutable: same content is a no-op, other content under the same version is an
        error, and versions only move forward (see `core.publishing`)."""
        ...

    async def get(self, tenant_id: str, agent_id: str, version: str) -> CompiledAgent | None: ...
    async def latest(self, tenant_id: str, agent_id: str) -> CompiledAgent | None: ...
    async def versions(self, tenant_id: str, agent_id: str) -> list[str]: ...
