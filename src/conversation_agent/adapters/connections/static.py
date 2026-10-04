from __future__ import annotations

from collections.abc import Mapping

from conversation_agent.core.errors import ConnectionNotFoundError
from conversation_agent.core.models.connections import ResolvedConnection


class StaticConnectionResolver:
    """In-process registry. Keys are `connection_id` (all tenants) or `(tenant_id, connection_id)`
    (tenant-specific, which wins)."""

    def __init__(self, connections: Mapping[str | tuple[str, str], ResolvedConnection]) -> None:
        self._connections = dict(connections)

    async def resolve(self, tenant_id: str, connection_id: str) -> ResolvedConnection:
        found = self._connections.get((tenant_id, connection_id)) or self._connections.get(
            connection_id
        )
        if found is None:
            raise ConnectionNotFoundError(f"no connection {connection_id!r} for this tenant")
        return found
