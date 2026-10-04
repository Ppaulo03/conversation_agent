from __future__ import annotations

import os
from collections.abc import Mapping

from conversation_agent.core.errors import SecretNotFoundError
from conversation_agent.core.models.connections import SecretValue


class InMemorySecretProvider:
    def __init__(self, secrets: Mapping[tuple[str, str], str]) -> None:
        self._secrets = dict(secrets)

    async def get(self, tenant_id: str, secret_ref: str) -> SecretValue:
        value = self._secrets.get((tenant_id, secret_ref))
        if value is None:
            raise SecretNotFoundError(f"secret {secret_ref!r} is not available for this tenant")
        return SecretValue(value)


class EnvSecretProvider:
    """`CA_SECRET__<TENANT>__<REF>` environment variables (upper-case, `-` -> `_`)."""

    def __init__(self, environ: Mapping[str, str] | None = None, prefix: str = "CA_SECRET") -> None:
        self._env = environ if environ is not None else os.environ
        self._prefix = prefix

    async def get(self, tenant_id: str, secret_ref: str) -> SecretValue:
        def norm(part: str) -> str:
            return part.upper().replace("-", "_")

        value = self._env.get(f"{self._prefix}__{norm(tenant_id)}__{norm(secret_ref)}")
        if not value:
            raise SecretNotFoundError(f"secret {secret_ref!r} is not available for this tenant")
        return SecretValue(value)
