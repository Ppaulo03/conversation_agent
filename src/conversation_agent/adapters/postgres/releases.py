"""Release state, history and decisions over PostgreSQL (see `core.releases` for the rules).

Every operation runs in ONE transaction under a per-agent advisory lock and writes, together with
the state change: the append-only history row and the admin audit entry. A release decision with
no trail, or a trail of a change that did not happen, cannot exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from conversation_agent.adapters.postgres.audit import insert_audit
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.models.audit import AuditEntry
from conversation_agent.core.releases import (
    ReleaseError,
    ReleaseEvidence,
    ReleaseState,
    bucket,
    pick_previous,
    target_version,
    validate_evidence,
)
from conversation_agent.core.versioning import Version
from conversation_agent.ports.registry import AgentRegistry


def _state_from(row: Any) -> ReleaseState:
    return ReleaseState(
        stable=row["stable"],
        candidate=row["candidate"],
        candidate_percent=row["candidate_percent"],
        withdrawn=frozenset(row["withdrawn"]),
    )


class PostgresReleaseResolver:
    """What the turn coordinator asks: which version does this conversation run on?"""

    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db

    async def target(
        self,
        tenant_id: str,
        agent_id: str,
        conversation_id: str,
        pinned: str | None,
        idle: bool,
    ) -> str | None:
        row = await self._db.pool.fetchrow(
            "SELECT stable, candidate, candidate_percent, withdrawn FROM agent_release_state "
            "WHERE tenant_id = $1 AND agent_id = $2",
            tenant_id,
            agent_id,
        )
        if row is None:
            return None
        return target_version(
            _state_from(row), pinned, idle, bucket(tenant_id, agent_id, conversation_id)
        )


@dataclass(frozen=True)
class ReleasePolicy:
    require_evidence: bool = True  # promoting or canarying needs a passing eval for that version


@dataclass(frozen=True)
class ReleaseEvent:
    id: int
    event: str
    version: str
    percent: int | None
    actor: str
    reason: str
    detail: dict[str, Any]


class ReleaseManager:
    def __init__(
        self,
        db: PostgresDatabase,
        registry: AgentRegistry,
        policy: ReleasePolicy | None = None,
    ) -> None:
        self._db = db
        self._registry = registry
        self._policy = policy or ReleasePolicy()

    # ------------------------------------------------------------------ reads

    async def state(self, tenant_id: str, agent_id: str) -> ReleaseState | None:
        row = await self._db.pool.fetchrow(
            "SELECT stable, candidate, candidate_percent, withdrawn FROM agent_release_state "
            "WHERE tenant_id = $1 AND agent_id = $2",
            tenant_id,
            agent_id,
        )
        return _state_from(row) if row else None

    async def history(self, tenant_id: str, agent_id: str, limit: int = 50) -> list[ReleaseEvent]:
        rows = await self._db.pool.fetch(
            "SELECT id, event, version, percent, actor, reason, detail FROM agent_release_history "
            "WHERE tenant_id = $1 AND agent_id = $2 ORDER BY id DESC LIMIT $3",
            tenant_id,
            agent_id,
            min(max(limit, 1), 500),
        )
        return [ReleaseEvent(**dict(r)) for r in rows]

    # ------------------------------------------------------------------ decisions

    async def promote(
        self,
        tenant_id: str,
        agent_id: str,
        version: str,
        *,
        actor: str,
        reason: str,
        evidence: ReleaseEvidence | None = None,
    ) -> ReleaseState:
        """`version` becomes the stable one (everyone new gets it)."""
        digest = await self._published_digest(tenant_id, agent_id, version)
        validate_evidence(evidence, agent_digest=digest, required=self._policy.require_evidence)
        async with self._tx(tenant_id, agent_id) as conn:
            current = await self._locked_state(conn, tenant_id, agent_id)
            if current is not None and version in current.withdrawn:
                raise ReleaseError(f"{version} was withdrawn and cannot be released again")
            candidate = current.candidate if current else None
            percent = current.candidate_percent if current else 0
            if candidate == version:  # the canary graduated
                candidate, percent = None, 0
            withdrawn = current.withdrawn if current else frozenset()
            await self._save(conn, tenant_id, agent_id, version, candidate, percent, withdrawn)
            await self._log(
                conn, tenant_id, agent_id, "promote", version, None, actor, reason,
                _evidence_detail(evidence, previous=current.stable if current else None),
            )  # fmt: skip
            return ReleaseState(version, candidate, percent, withdrawn)

    async def start_canary(
        self,
        tenant_id: str,
        agent_id: str,
        version: str,
        percent: int,
        *,
        actor: str,
        reason: str,
        evidence: ReleaseEvidence | None = None,
    ) -> ReleaseState:
        """Offer `version` to `percent`% of conversations (a stable hash, so it is reproducible)."""
        if not 1 <= percent <= 100:
            raise ReleaseError("a canary needs a percentage between 1 and 100")
        digest = await self._published_digest(tenant_id, agent_id, version)
        validate_evidence(evidence, agent_digest=digest, required=self._policy.require_evidence)
        async with self._tx(tenant_id, agent_id) as conn:
            current = await self._locked_state(conn, tenant_id, agent_id)
            if current is None:
                raise ReleaseError("there is no stable release yet: promote a version first")
            if version in current.withdrawn:
                raise ReleaseError(f"{version} was withdrawn and cannot be released again")
            if Version(version) <= Version(current.stable):
                raise ReleaseError(f"a canary must be newer than the stable {current.stable}")
            await self._save(
                conn, tenant_id, agent_id, current.stable, version, percent, current.withdrawn
            )
            await self._log(
                conn, tenant_id, agent_id, "canary", version, percent, actor, reason,
                _evidence_detail(evidence),
            )  # fmt: skip
            return ReleaseState(current.stable, version, percent, current.withdrawn)

    async def set_canary_percent(
        self, tenant_id: str, agent_id: str, percent: int, *, actor: str, reason: str
    ) -> ReleaseState:
        """Widen or narrow the canary (0 pauses it). Widening only ever ADDS conversations."""
        if not 0 <= percent <= 100:
            raise ReleaseError("a percentage is between 0 and 100")
        async with self._tx(tenant_id, agent_id) as conn:
            current = await self._locked_state(conn, tenant_id, agent_id)
            if current is None or current.candidate is None:
                raise ReleaseError("there is no canary to change")
            await self._save(
                conn, tenant_id, agent_id, current.stable, current.candidate, percent,
                current.withdrawn,
            )  # fmt: skip
            await self._log(
                conn, tenant_id, agent_id, "canary_percent", current.candidate, percent, actor,
                reason, {"from": current.candidate_percent},
            )  # fmt: skip
            return ReleaseState(current.stable, current.candidate, percent, current.withdrawn)

    async def rollback(
        self, tenant_id: str, agent_id: str, *, actor: str, reason: str
    ) -> ReleaseState:
        """Pull what is wrong, no evidence needed (it is an emergency): a running canary is
        withdrawn; otherwise the stable version is withdrawn and the previous one is restored."""
        async with self._tx(tenant_id, agent_id) as conn:
            current = await self._locked_state(conn, tenant_id, agent_id)
            if current is None:
                raise ReleaseError("there is nothing released to roll back")
            if current.candidate is not None:
                bad = current.candidate
                withdrawn = current.withdrawn | {bad}
                await self._save(conn, tenant_id, agent_id, current.stable, None, 0, withdrawn)
                await self._log(
                    conn, tenant_id, agent_id, "abort_canary", bad, None, actor, reason, {}
                )
                return ReleaseState(current.stable, None, 0, withdrawn)
            promoted = [
                r["version"]
                for r in await conn.fetch(
                    "SELECT version FROM agent_release_history WHERE tenant_id = $1 AND "
                    "agent_id = $2 AND event = 'promote' ORDER BY id",
                    tenant_id,
                    agent_id,
                )
            ]
            withdrawn = current.withdrawn | {current.stable}
            previous = pick_previous(promoted, current.stable, withdrawn)
            if previous is None:
                raise ReleaseError("there is no earlier stable version to go back to")
            await self._save(conn, tenant_id, agent_id, previous, None, 0, withdrawn)
            await self._log(
                conn, tenant_id, agent_id, "rollback", previous, None, actor, reason,
                {"withdrawn": current.stable},
            )  # fmt: skip
            return ReleaseState(previous, None, 0, withdrawn)

    # ------------------------------------------------------------------ internals

    def _tx(self, tenant_id: str, agent_id: str) -> _Transaction:
        return _Transaction(self._db, f"release/{tenant_id}/{agent_id}")

    async def _published_digest(self, tenant_id: str, agent_id: str, version: str) -> str:
        compiled = await self._registry.get(tenant_id, agent_id, version)
        if compiled is None:
            raise ReleaseError(f"{agent_id} {version} is not published")
        return compiled.digest

    @staticmethod
    async def _locked_state(conn: Any, tenant_id: str, agent_id: str) -> ReleaseState | None:
        row = await conn.fetchrow(
            "SELECT stable, candidate, candidate_percent, withdrawn FROM agent_release_state "
            "WHERE tenant_id = $1 AND agent_id = $2",
            tenant_id,
            agent_id,
        )
        return _state_from(row) if row else None

    @staticmethod
    async def _save(
        conn: Any,
        tenant_id: str,
        agent_id: str,
        stable: str,
        candidate: str | None,
        percent: int,
        withdrawn: frozenset[str],
    ) -> None:
        await conn.execute(
            "INSERT INTO agent_release_state (tenant_id, agent_id, stable, candidate, "
            "candidate_percent, withdrawn) VALUES ($1,$2,$3,$4,$5,$6) "
            "ON CONFLICT (tenant_id, agent_id) DO UPDATE SET stable = $3, candidate = $4, "
            "candidate_percent = $5, withdrawn = $6, updated_at = clock_timestamp()",
            tenant_id,
            agent_id,
            stable,
            candidate,
            percent,
            sorted(withdrawn),
        )

    @staticmethod
    async def _log(
        conn: Any,
        tenant_id: str,
        agent_id: str,
        event: str,
        version: str,
        percent: int | None,
        actor: str,
        reason: str,
        detail: dict[str, Any],
    ) -> None:
        await conn.execute(
            "INSERT INTO agent_release_history (tenant_id, agent_id, event, version, percent, "
            "actor, reason, detail) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)",
            tenant_id,
            agent_id,
            event,
            version,
            percent,
            actor,
            reason,
            detail,
        )
        await insert_audit(
            conn,
            AuditEntry(
                tenant_id=tenant_id,
                actor=actor,
                action=f"release.{event}",
                subject_type="agent_version",
                subject_id=f"{agent_id}@{version}",
                details={
                    "reason": reason,
                    **({"percent": percent} if percent is not None else {}),
                    **{k: v for k, v in detail.items() if isinstance(v, str | int | bool)},
                },
            ),
        )


def _evidence_detail(evidence: ReleaseEvidence | None, **extra: Any) -> dict[str, Any]:
    detail: dict[str, Any] = {k: v for k, v in extra.items() if v is not None}
    if evidence is not None:
        detail.update(
            suite=evidence.suite,
            suite_digest=evidence.suite_digest,
            passed=evidence.passed,
            total=evidence.total,
        )
    return detail


class _Transaction:
    """A connection + transaction holding the per-agent advisory lock (one release decision at a
    time for one agent, so the state we read cannot move under us)."""

    def __init__(self, db: PostgresDatabase, lock_key: str) -> None:
        self._db = db
        self._key = lock_key
        self._conn: Any = None
        self._tx: Any = None

    async def __aenter__(self) -> Any:
        self._conn = await self._db.pool.acquire()
        self._tx = self._conn.transaction()
        await self._tx.start()
        await self._conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", self._key)
        return self._conn

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if exc_type is None:
                await self._tx.commit()
            else:
                await self._tx.rollback()
        finally:
            await self._db.pool.release(self._conn)
