from __future__ import annotations

from typing import Protocol

from conversation_agent.core.models.connections import ResolvedConnection


class ConnectionResolver(Protocol):
    async def resolve(self, tenant_id: str, connection_id: str) -> ResolvedConnection:
        """Raises `ConnectionNotFoundError` when the tenant has no such connection."""
        ...
