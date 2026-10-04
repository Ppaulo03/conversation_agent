from __future__ import annotations

from typing import Protocol

from conversation_agent.core.models.connections import SecretValue


class SecretProvider(Protocol):
    async def get(self, tenant_id: str, secret_ref: str) -> SecretValue:
        """Raises `SecretNotFoundError`. Secrets never enter prompts, traces or `args_hash`."""
        ...
