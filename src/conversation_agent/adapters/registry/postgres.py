"""Durable AgentRegistry over PostgreSQL.

The MANIFEST is what is stored (a Python object graph cannot be): loading recompiles it and
checks the digest against the one recorded at publish time, so a changed compiler, a tampered
row or a manifest that no longer means what it meant is detected instead of silently running.
Rows are immutable at the database level (trigger), not only by convention.
"""

from __future__ import annotations

from typing import Any

from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.compiler import CompiledAgent, compile_manifest
from conversation_agent.core.errors import (
    PublishError,
    RegistryCompatibilityError,
    RegistryIntegrityError,
)
from conversation_agent.core.publishing import plan_publish
from conversation_agent.core.versioning import SUPPORTED_COMPILER_VERSIONS, Version
from conversation_agent.ports.registry import PublishedAgent


class PostgresAgentRegistry:
    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db
        self._cache: dict[tuple[str, str], CompiledAgent] = {}  # published rows never change

    async def publish(self, compiled: CompiledAgent) -> PublishedAgent:
        if compiled.manifest is None:
            raise PublishError(
                "only agents compiled from a manifest can be published durably "
                "(a Python object graph cannot be stored); use the in-memory registry"
            )
        async with self._db.pool.acquire() as conn, conn.transaction():
            # one publisher per agent at a time: the "latest" we compare with cannot move
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", compiled.agent_id)
            rows = await conn.fetch(
                "SELECT version FROM published_agents WHERE agent_id=$1", compiled.agent_id
            )
            existing = [await self._load(r["version"], compiled.agent_id, conn) for r in rows]
            plan = plan_publish(existing, compiled)
            if plan.action == "new":
                await conn.execute(
                    "INSERT INTO published_agents (agent_id, version, digest, manifest_json, "
                    "schema_version, compiler_version) VALUES ($1,$2,$3,$4,$5,$6)",
                    compiled.agent_id,
                    compiled.version,
                    compiled.digest,
                    # exclude_unset: a default never written must not become an explicit one
                    compiled.manifest.model_dump(mode="json", by_alias=True, exclude_unset=True),
                    compiled.schema_version,
                    compiled.compiler_version,
                )
        self._cache[(compiled.agent_id, compiled.version)] = compiled
        return PublishedAgent(
            agent_id=compiled.agent_id,
            version=compiled.version,
            digest=compiled.digest,
            breaking_changes=plan.breaking,
            created=plan.action == "new",
        )

    async def get(self, agent_id: str, version: str) -> CompiledAgent | None:
        cached = self._cache.get((agent_id, version))
        if cached is not None:
            return cached
        async with self._db.pool.acquire() as conn:
            return await self._load(version, agent_id, conn, missing_ok=True)

    async def latest(self, agent_id: str) -> CompiledAgent | None:
        versions = await self.versions(agent_id)
        return await self.get(agent_id, versions[-1]) if versions else None

    async def versions(self, agent_id: str) -> list[str]:
        rows = await self._db.pool.fetch(
            "SELECT version FROM published_agents WHERE agent_id=$1", agent_id
        )
        return sorted((r["version"] for r in rows), key=Version)

    async def _load(
        self, version: str, agent_id: str, conn: Any, *, missing_ok: bool = False
    ) -> CompiledAgent:
        cached = self._cache.get((agent_id, version))
        if cached is not None:
            return cached
        row = await conn.fetchrow(
            "SELECT digest, manifest_json, schema_version, compiler_version FROM published_agents "
            "WHERE agent_id=$1 AND version=$2",
            agent_id,
            version,
        )
        if row is None:
            if missing_ok:
                return None  # type: ignore[return-value]
            raise RegistryIntegrityError(f"{agent_id} {version} vanished from the registry")
        if row["compiler_version"] not in SUPPORTED_COMPILER_VERSIONS:
            raise RegistryCompatibilityError(
                f"{agent_id} {version} was published with compiler {row['compiler_version']}; "
                f"this build supports {sorted(SUPPORTED_COMPILER_VERSIONS)}"
            )
        compiled = compile_manifest(row["manifest_json"])
        if (compiled.agent_id, compiled.version, compiled.schema_version) != (
            agent_id,
            version,
            row["schema_version"],
        ):
            raise RegistryIntegrityError(
                f"row {agent_id} {version} holds a manifest for {compiled.agent_id} "
                f"{compiled.version} (format {compiled.schema_version}): not what was published"
            )
        if compiled.digest != row["digest"]:
            raise RegistryIntegrityError(
                f"{agent_id} {version} no longer compiles to its published digest: refusing to "
                "run a definition that is not the one that was published"
            )
        self._cache[(agent_id, version)] = compiled
        return compiled
