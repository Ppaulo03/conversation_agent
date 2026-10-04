from __future__ import annotations

from conversation_agent.core.compiler import CompiledAgent
from conversation_agent.core.models.audit import AuditEntry
from conversation_agent.ports.audit import AuditLog
from conversation_agent.ports.registry import AgentRegistry, PublishedAgent


class AuditedAgentRegistry:
    """Publishing is an administrative act: every attempt leaves a trail with who did it."""

    def __init__(self, inner: AgentRegistry, audit: AuditLog, *, actor: str) -> None:
        self._inner = inner
        self._audit = audit
        self._actor = actor

    async def publish(self, tenant_id: str, compiled: CompiledAgent) -> PublishedAgent:
        subject = f"{compiled.agent_id}@{compiled.version}"
        try:
            published = await self._inner.publish(tenant_id, compiled)
        except Exception as exc:
            await self._audit.record(
                AuditEntry(
                    tenant_id=tenant_id,
                    actor=self._actor,
                    action="agent.publish",
                    subject_type="agent_version",
                    subject_id=subject,
                    outcome="refused",
                    details={"error": type(exc).__name__},
                )
            )
            raise
        await self._audit.record(
            AuditEntry(
                tenant_id=tenant_id,
                actor=self._actor,
                action="agent.publish",
                subject_type="agent_version",
                subject_id=subject,
                details={
                    "digest": compiled.digest,
                    "created": published.created,
                    "breaking_changes": len(published.breaking_changes),
                },
            )
        )
        return published

    async def get(self, tenant_id: str, agent_id: str, version: str) -> CompiledAgent | None:
        return await self._inner.get(tenant_id, agent_id, version)

    async def latest(self, tenant_id: str, agent_id: str) -> CompiledAgent | None:
        return await self._inner.latest(tenant_id, agent_id)

    async def versions(self, tenant_id: str, agent_id: str) -> list[str]:
        return await self._inner.versions(tenant_id, agent_id)
