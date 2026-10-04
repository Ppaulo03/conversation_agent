"""Integrity check of the durable state: run it after a restore, an incident, before a release.

It asks the database whether the invariants that the runtime keeps by construction still hold. Each
check is a query that returns the offending identifiers (never content); a healthy database returns
none. It READS only: a violation is something a person must understand, never something to
"repair" blindly, because the fixes the runtime would make by itself (replay, reconcile) are exactly
what the workers do once started.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.migrator import Migrator
from conversation_agent.core.errors import ConversationAgentError

SAMPLE = 5

# code -> (what is wrong, SQL returning one identifier column named `id` per offending row)
CHECKS: dict[str, tuple[str, str]] = {
    "confirmed_action_without_invocation": (
        "a confirmed action has no prepared invocation (INV-005: never an orphan CONFIRMED)",
        "SELECT p.tenant_id || '/' || p.action_id AS id FROM pending_actions p "
        "WHERE p.status = 'CONFIRMED' AND NOT EXISTS (SELECT 1 FROM tool_invocations i "
        "WHERE i.tenant_id = p.tenant_id AND i.action_id = p.action_id)",
    ),
    "invocation_without_its_action": (
        "an invocation names an action that does not exist",
        "SELECT i.tenant_id || '/' || i.invocation_id AS id FROM tool_invocations i "
        "WHERE i.action_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM pending_actions p "
        "WHERE p.tenant_id = i.tenant_id AND p.action_id = i.action_id) "
        "AND i.args_hash <> 'erased'",
    ),
    "finished_invocation_without_finish_time": (
        "a finished invocation has no finished_at",
        "SELECT tenant_id || '/' || invocation_id AS id FROM tool_invocations "
        "WHERE status IN ('SUCCEEDED', 'FAILED', 'RECONCILED') AND finished_at IS NULL",
    ),
    "completed_turn_without_completion_step": (
        "a completed turn has no TURN_COMPLETED step in its journal",
        "SELECT t.tenant_id || '/' || t.turn_id AS id FROM turns t WHERE t.status = 'COMPLETED' "
        "AND NOT EXISTS (SELECT 1 FROM turn_journal j WHERE j.tenant_id = t.tenant_id "
        "AND j.turn_id = t.turn_id AND j.step_type = 'TURN_COMPLETED')",
    ),
    "consumed_event_without_turn": (
        "an inbound event was consumed by no turn",
        "SELECT tenant_id || '/' || event_id AS id FROM inbox_events "
        "WHERE status = 'CONSUMED' AND turn_id IS NULL",
    ),
    "repeated_idempotency_key": (
        "two outbound rows share an idempotency key: the second would be a duplicate send",
        "SELECT tenant_id || '/' || idempotency_key AS id FROM outbox_messages "
        "GROUP BY tenant_id, idempotency_key HAVING count(*) > 1",
    ),
    "lease_without_expiry": (
        "a conversation has a lease owner but no expiry (it could never be taken over)",
        "SELECT tenant_id || '/' || conversation_id AS id FROM conversation_states "
        "WHERE lease_owner IS NOT NULL AND lease_expires_at IS NULL",
    ),
    "conversation_pinned_to_unpublished_version": (
        "a conversation is pinned to an agent version that is not published (a partial restore?)",
        "SELECT c.tenant_id || '/' || c.conversation_id AS id FROM conversation_states c "
        "WHERE c.agent_id IS NOT NULL AND c.agent_version IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM published_agents a WHERE a.tenant_id = c.tenant_id "
        "AND a.agent_id = c.agent_id AND a.version = c.agent_version)",
    ),
    "release_points_to_unpublished_version": (
        "a release's stable or candidate version is not published",
        "SELECT r.tenant_id || '/' || r.agent_id AS id FROM agent_release_state r "
        "WHERE NOT EXISTS (SELECT 1 FROM published_agents a WHERE a.tenant_id = r.tenant_id "
        "AND a.agent_id = r.agent_id AND a.version = r.stable) "
        "OR (r.candidate IS NOT NULL AND NOT EXISTS (SELECT 1 FROM published_agents a "
        "WHERE a.tenant_id = r.tenant_id AND a.agent_id = r.agent_id AND a.version = r.candidate))",
    ),
    "release_stable_is_withdrawn": (
        "a release serves a version that is also marked withdrawn",
        "SELECT tenant_id || '/' || agent_id AS id FROM agent_release_state "
        "WHERE stable = ANY(withdrawn) OR candidate = ANY(withdrawn)",
    ),
}


@dataclass(frozen=True)
class Violation:
    code: str
    meaning: str
    count: int
    sample: tuple[str, ...]


@dataclass(frozen=True)
class IntegrityReport:
    violations: tuple[Violation, ...] = ()
    schema_problem: str | None = None
    checked: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.violations and self.schema_problem is None


async def verify_integrity(db: PostgresDatabase) -> IntegrityReport:
    schema_problem: str | None = None
    try:
        await Migrator(db.pool).ensure_current()
    except ConversationAgentError as exc:
        schema_problem = f"{type(exc).__name__}: {exc}"
        return IntegrityReport(schema_problem=schema_problem)  # the queries assume this schema
    violations: list[Violation] = []
    async with db.pool.acquire() as conn:
        for code, (meaning, sql) in CHECKS.items():
            rows = await conn.fetch(sql)
            if rows:
                ids = tuple(str(r["id"]) for r in rows[:SAMPLE])
                violations.append(Violation(code, meaning, len(rows), ids))
    return IntegrityReport(tuple(violations), schema_problem, tuple(CHECKS))
