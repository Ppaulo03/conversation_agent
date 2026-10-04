"""Durable, TENANT-SCOPED AgentRegistry over PostgreSQL.

The MANIFEST is what is stored (a Python object graph cannot be): loading looks up the loader of
the compiler version that published it, recompiles, and checks the identity of the row (tenant,
agent, version, format) and the digest, so a changed compiler, a tampered row or a manifest that
no longer means what it meant is detected instead of silently running. Rows are immutable at the
database level (trigger), not only by convention.
"""

from __future__ import annotations

from typing import Any

from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.compiler import COMPILERS, CompiledAgent, CompileError
from conversation_agent.core.errors import (
    PublishError,
    RegistryCompatibilityError,
    RegistryIntegrityError,
)
from conversation_agent.core.publishing import plan_publish
from conversation_agent.core.versioning import Version
from conversation_agent.ports.registry import PublishedAgent

_Key = tuple[str, str, str]  # (tenant, agent, version)


class PostgresAgentRegistry:
    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db
        self._cache: dict[_Key, CompiledAgent] = {}  # published rows never change

    async def publish(self, tenant_id: str, compiled: CompiledAgent) -> PublishedAgent:
        if compiled.manifest is None:
            raise PublishError(
                "only agents compiled from a manifest can be published durably "
                "(a Python object graph cannot be stored); use the in-memory registry"
            )
        async with self._db.pool.acquire() as conn, conn.transaction():
            # one publisher per (tenant, agent) at a time: the "latest" we compare with cannot move
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"{tenant_id}/{compiled.agent_id}"
            )
            rows = await conn.fetch(
                "SELECT version FROM published_agents WHERE tenant_id=$1 AND agent_id=$2",
                tenant_id,
                compiled.agent_id,
            )
            existing = [
                await self._load(tenant_id, compiled.agent_id, r["version"], conn) for r in rows
            ]
            plan = plan_publish([e for e in existing if e is not None], compiled)
            if plan.action == "new":
                await conn.execute(
                    "INSERT INTO published_agents (tenant_id, agent_id, version, digest, "
                    "manifest_digest, manifest_json, schema_version, compiler_version) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
                    tenant_id,
                    compiled.agent_id,
                    compiled.version,
                    compiled.digest,
                    compiled.manifest_digest,
                    # exclude_unset: a default never written must not become an explicit one
                    compiled.manifest.model_dump(mode="json", by_alias=True, exclude_unset=True),
                    compiled.schema_version,
                    compiled.compiler_version,
                )
        self._cache[(tenant_id, compiled.agent_id, compiled.version)] = compiled
        return PublishedAgent(
            tenant_id=tenant_id,
            agent_id=compiled.agent_id,
            version=compiled.version,
            digest=compiled.digest,
            breaking_changes=plan.breaking,
            created=plan.action == "new",
        )

    async def get(self, tenant_id: str, agent_id: str, version: str) -> CompiledAgent | None:
        cached = self._cache.get((tenant_id, agent_id, version))
        if cached is not None:
            return cached
        async with self._db.pool.acquire() as conn:
            return await self._load(tenant_id, agent_id, version, conn)

    async def latest(self, tenant_id: str, agent_id: str) -> CompiledAgent | None:
        versions = await self.versions(tenant_id, agent_id)
        return await self.get(tenant_id, agent_id, versions[-1]) if versions else None

    async def versions(self, tenant_id: str, agent_id: str) -> list[str]:
        rows = await self._db.pool.fetch(
            "SELECT version FROM published_agents WHERE tenant_id=$1 AND agent_id=$2",
            tenant_id,
            agent_id,
        )
        return sorted((r["version"] for r in rows), key=Version)

    async def _load(
        self, tenant_id: str, agent_id: str, version: str, conn: Any
    ) -> CompiledAgent | None:
        cached = self._cache.get((tenant_id, agent_id, version))
        if cached is not None:
            return cached
        row = await conn.fetchrow(
            "SELECT digest, manifest_digest, manifest_json, schema_version, compiler_version "
            "FROM published_agents "
            "WHERE tenant_id=$1 AND agent_id=$2 AND version=$3",
            tenant_id,
            agent_id,
            version,
        )
        if row is None:
            return None
        loader = COMPILERS.get(row["compiler_version"])
        if loader is None:
            raise RegistryCompatibilityError(
                f"{agent_id} {version} was published with compiler {row['compiler_version']}; "
                f"this build has loaders for {sorted(COMPILERS)}"
            )
        try:
            compiled = loader(row["manifest_json"])
        except CompileError as exc:  # a stored manifest that no longer compiles is corruption
            raise RegistryIntegrityError(
                f"{agent_id} {version} holds a manifest that does not compile: {exc.codes}"
            ) from exc
        if (compiled.agent_id, compiled.version, compiled.schema_version) != (
            agent_id,
            version,
            row["schema_version"],
        ):
            raise RegistryIntegrityError(
                f"row {agent_id} {version} holds a manifest for {compiled.agent_id} "
                f"{compiled.version} (format {compiled.schema_version}): not what was published"
            )
        if compiled.manifest_digest != row["manifest_digest"]:
            raise RegistryIntegrityError(
                f"{agent_id} {version} is not byte-for-byte the manifest that was published "
                "(metadata changed)"
            )
        if compiled.digest != row["digest"]:
            raise RegistryIntegrityError(
                f"{agent_id} {version} no longer compiles to its published digest: refusing to "
                "run a definition that is not the one that was published"
            )
        self._cache[(tenant_id, agent_id, version)] = compiled
        return compiled
