from __future__ import annotations

from conversation_agent.core.compiler import CompiledAgent
from conversation_agent.core.publishing import plan_publish
from conversation_agent.core.versioning import Version
from conversation_agent.ports.registry import PublishedAgent


class InMemoryAgentRegistry:
    """Process-local, tenant-scoped registry (tests, single-process deployments, Python agents)."""

    def __init__(self) -> None:
        self._agents: dict[tuple[str, str], dict[str, CompiledAgent]] = {}

    async def publish(self, tenant_id: str, compiled: CompiledAgent) -> PublishedAgent:
        versions = self._agents.setdefault((tenant_id, compiled.agent_id), {})
        plan = plan_publish(list(versions.values()), compiled)
        if plan.action == "new":
            versions[compiled.version] = compiled
        return PublishedAgent(
            tenant_id=tenant_id,
            agent_id=compiled.agent_id,
            version=compiled.version,
            digest=compiled.digest,
            breaking_changes=plan.breaking,
            created=plan.action == "new",
        )

    async def get(self, tenant_id: str, agent_id: str, version: str) -> CompiledAgent | None:
        return self._agents.get((tenant_id, agent_id), {}).get(version)

    async def latest(self, tenant_id: str, agent_id: str) -> CompiledAgent | None:
        versions = self._agents.get((tenant_id, agent_id), {})
        return versions[max(versions, key=Version)] if versions else None

    async def versions(self, tenant_id: str, agent_id: str) -> list[str]:
        return sorted(self._agents.get((tenant_id, agent_id), {}), key=Version)
