"""Administrative audit (DESIGN 38): append-only in the database itself, content-free, tenant
scoped, and written by the operations that need it (publishing, ownership changes)."""

from __future__ import annotations

import asyncpg
import pytest
from pydantic import ValidationError

from conversation_agent.adapters.audit.memory import InMemoryAuditLog
from conversation_agent.adapters.audit.registry import AuditedAgentRegistry
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.postgres.audit import PostgresAuditLog
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.registry.memory import InMemoryAgentRegistry
from conversation_agent.core.errors import PublishError
from conversation_agent.core.models.audit import AuditEntry, subject_ref
from conversation_agent.engine.ownership import OwnershipService, OwnershipTransitionError
from postgres.world import KEY, World, event
from support.support_domain import compiled_support

TENANT = KEY.tenant_id


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


def entry(**kw: object) -> AuditEntry:
    base: dict[str, object] = {
        "tenant_id": TENANT,
        "actor": "ops@example.com",
        "action": "agent.publish",
        "subject_type": "agent_version",
        "subject_id": "support-demo@0.1.0",
        "details": {"digest": "abc"},
    }
    return AuditEntry.model_validate({**base, **kw})


# --- the entry itself ---


@pytest.mark.parametrize("key", ["text", "message", "content", "args", "email", "phone", "result"])
def test_a_detail_that_names_content_is_refused(key: str) -> None:
    with pytest.raises(ValidationError, match="not allowed"):
        entry(details={key: "anything"})


def test_details_are_scalars_bounded_and_redacted() -> None:
    clean = entry(
        details={"note": "contact ana@example.com asked", "count": 3, "ok": True, "ids": ["a", "b"]}
    )
    assert clean.details["note"] == "contact [REDACTED_EMAIL] asked"
    assert clean.details["count"] == 3 and clean.details["ids"] == ["a", "b"]
    assert len(entry(details={"long": "x" * 5000}).details["long"]) == 200
    for bad in ({"nested": {"a": 1}}, {"Bad-Key": 1}, {f"k{i}": i for i in range(30)}):
        with pytest.raises(ValidationError):
            entry(details=bad)


def test_a_person_reference_is_stable_scoped_and_not_the_identifier() -> None:
    ref = subject_ref("t1", "5511999990000")
    assert ref == subject_ref("t1", "5511999990000") and "5511999990000" not in ref
    assert ref != subject_ref("t2", "5511999990000")  # the same contact in another tenant differs


# --- the durable log ---


async def test_the_trail_is_append_only_in_the_database_itself(db: PostgresDatabase) -> None:
    log = PostgresAuditLog(db)
    await log.record(entry())
    with pytest.raises(asyncpg.PostgresError, match="append-only"):
        await db.pool.execute("UPDATE admin_audit SET actor = 'someone else'")
    with pytest.raises(asyncpg.PostgresError, match="append-only"):
        await db.pool.execute("DELETE FROM admin_audit")


async def test_each_log_lists_newest_first_for_one_tenant_only(db: PostgresDatabase) -> None:
    for log in (PostgresAuditLog(db), InMemoryAuditLog()):
        await log.record(entry(action="agent.publish", subject_id="a@1.0.0"))
        await log.record(entry(action="release.promote", subject_id="a@1.0.0"))
        await log.record(entry(tenant_id="other-tenant", action="agent.publish"))
        records = await log.list(TENANT)
        assert [r.action for r in records] == ["release.promote", "agent.publish"]
        assert [r.action for r in await log.list(TENANT, action="agent.publish")] == [
            "agent.publish"
        ]
        assert len(await log.list("other-tenant")) == 1 and await log.list("nobody") == []
        assert len(await log.list(TENANT, subject_id="a@1.0.0", limit=1)) == 1
        await db.pool.execute("TRUNCATE admin_audit")


# --- who writes it ---


async def test_publishing_leaves_a_trail_with_the_actor_and_refusals_too(
    db: PostgresDatabase,
) -> None:
    log = PostgresAuditLog(db)
    registry = AuditedAgentRegistry(InMemoryAgentRegistry(), log, actor="deploy-bot")
    compiled = compiled_support()
    await registry.publish(TENANT, compiled)
    changed = compiled.manifest.model_copy(  # type: ignore[union-attr]
        update={"persona": "another persona under the SAME version"}
    )
    from conversation_agent.core.compiler import compile_manifest

    with pytest.raises(PublishError):
        await registry.publish(TENANT, compile_manifest(changed))
    done, refused = (await log.list(TENANT, action="agent.publish"))[::-1]
    assert (done.actor, done.outcome, done.subject_id) == ("deploy-bot", "ok", "support-demo@0.1.0")
    assert done.details["digest"] == compiled.digest and done.details["created"] is True
    assert refused.outcome == "refused" and refused.details["error"] == "VersionConflictError"


async def start(world: World) -> None:
    await world.inbox.insert_if_absent(event("e0", "oi", clock=world.clock))
    await world.coordinator("c", FakeLLM([text_response("Olá!")])).run_once()


async def test_an_ownership_change_is_audited_without_naming_the_conversation(
    world: World,
) -> None:
    await start(world)
    log = PostgresAuditLog(world.db)
    ops = OwnershipService(world.leases, world.uows, owner="ops-svc", audit=log)
    await ops.assign_human(KEY, actor="maria@example.com")
    await ops.assign_human(KEY, actor="maria@example.com")  # already there: nothing changed
    with pytest.raises(OwnershipTransitionError):
        await ops.request_handoff(KEY, actor="maria@example.com")  # refused: no trail of a change
    await ops.return_to_bot(KEY)  # no actor given: the service identity is recorded
    first, second = (await log.list(TENANT, action="conversation.ownership"))[::-1]
    assert (first.actor, first.details) == ("maria@example.com", {"from": "BOT", "to": "HUMAN"})
    assert (second.actor, second.details) == ("ops-svc", {"from": "HUMAN", "to": "BOT"})
    assert first.subject_id == subject_ref(TENANT, KEY.conversation_id)
    assert KEY.conversation_id not in first.subject_id


async def test_an_ownership_change_and_its_trail_commit_together_or_not_at_all(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conversation_agent.core.models.audit import PseudonymKeyError, configure_pseudonym_key

    await start(world)
    ops = OwnershipService(world.leases, world.uows, owner="ops", audit=PostgresAuditLog(world.db))

    configure_pseudonym_key(None)  # no key: the reference cannot be written...
    with pytest.raises(PseudonymKeyError):
        await ops.assign_human(KEY)
    assert await world.db.pool.fetchval("SELECT ownership FROM conversation_states") == "BOT"
    # ...so the change did NOT happen (it used to stay, unaudited)

    configure_pseudonym_key("test-pseudonym-key-0123456789abcdef-test")

    async def broken(conn: object, entry: object) -> None:
        raise OSError("audit table unavailable")

    monkeypatch.setattr("conversation_agent.adapters.postgres.uow.insert_audit", broken)
    with pytest.raises(OSError):
        await ops.assign_human(KEY)
    assert await world.db.pool.fetchval("SELECT ownership FROM conversation_states") == "BOT"
    assert await world.count("admin_audit") == 0  # and no half-written trail either

    monkeypatch.undo()
    await ops.assign_human(KEY)
    assert await world.db.pool.fetchval("SELECT ownership FROM conversation_states") == "HUMAN"
    assert await world.count("admin_audit", "action = 'conversation.ownership'") == 1
