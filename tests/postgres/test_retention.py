"""Retention and erasure (LGPD, DESIGN 38) on real data produced by the runtime: a conversation
that proposed, confirmed and executed a booking leaves rows in every table. Erasing the contact
removes what names them, keeps what other guarantees need (as content-free tombstones), refuses
while something is still in motion, and leaves a trail with no content."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from pydantic import ValidationError

from conftest import ApiHandle
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.postgres.audit import PostgresAuditLog
from conversation_agent.adapters.postgres.conversations import ensure_conversation
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.retention import RetentionService
from conversation_agent.core.models.audit import subject_ref
from conversation_agent.core.models.conversation import ConversationIdentity
from conversation_agent.core.retention import RetentionPolicy
from postgres.test_protected_actions import answer, coord, deliver_prompt
from postgres.world import KEY, World, event
from support.builders import IDENTITY

TENANT = IDENTITY.tenant_id
CONTACT = IDENTITY.contact_id
OTHER = ConversationIdentity(
    tenant_id=TENANT,
    channel_id="cli",
    conversation_id="conv-other",
    session_id="sess-other",
    contact_id="contact-other",
)


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


@pytest.fixture
def service(world: World) -> RetentionService:
    return RetentionService(world.db)


async def propose(world: World, api: ApiHandle) -> None:
    await world.inbox.insert_if_absent(
        event("e-1", "Quero marcar um corte amanhã às 10h", clock=world.clock)
    )
    run = await coord(world, api, "w", FakeLLM([]), flows=True).process_conversation(KEY)
    assert run.status == "done"


async def full_conversation(world: World, api: ApiHandle) -> None:
    """Propose, deliver the prompt, confirm, execute and deliver the answer."""
    await propose(world, api)
    await deliver_prompt(world)
    await answer(
        world, api, "sim", FakeLLM([text_response("Agendado para terça às 10h!")]), flows=True
    )
    await world.outbox_worker("sender").run_once()


async def other_contact(world: World) -> None:
    await ensure_conversation(world.db.pool, OTHER, world.clock.now())
    await world.inbox.insert_if_absent(
        event("e-other", "oi, sou outra pessoa", clock=world.clock).model_copy(
            update={
                "conversation_id": OTHER.conversation_id,
                "contact_id": OTHER.contact_id,
                "session_id": OTHER.session_id,
            }
        )
    )


async def where_the_contact_still_appears(world: World) -> dict[str, int]:
    """Plain-text search of EVERY table for the identifiers of the erased person."""
    found: dict[str, int] = {}
    tables = await world.db.pool.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
        "AND table_name NOT IN ('schema_migrations', 'published_agents', 'tenant_retention')"
    )
    for row in tables:
        name = row["table_name"]
        count = await world.db.pool.fetchval(
            f"SELECT count(*) FROM {name} t WHERE t::text ILIKE '%{CONTACT}%' "
            f"OR t::text ILIKE '%{IDENTITY.conversation_id}%'"
        )
        if count:
            found[name] = int(count)
    return found


# --- locate ---


async def test_a_contacts_data_is_found_through_tenant_and_contact(
    world: World, api: ApiHandle, service: RetentionService
) -> None:
    await full_conversation(world, api)
    await other_contact(world)
    mine = await service.locate_contact(TENANT, CONTACT)
    assert all(mine.counts[t] >= 1 for t in (
        "conversation_states", "turns", "turn_journal", "inbox_events", "outbox_messages",
        "tool_invocations", "pending_actions",
    ))  # fmt: skip
    theirs = await service.locate_contact(TENANT, OTHER.contact_id)
    assert theirs.counts["inbox_events"] == 1 and theirs.counts["tool_invocations"] == 0
    assert (await service.locate_contact(TENANT, "nobody")).total == 0
    assert (await service.locate_contact("another-tenant", CONTACT)).total == 0


# --- erase ---


async def test_erasing_a_contact_removes_everything_that_names_them(
    world: World, api: ApiHandle, service: RetentionService
) -> None:
    await full_conversation(world, api)
    await other_contact(world)
    assert await where_the_contact_still_appears(world)  # sanity: the data is there
    before_other = await service.locate_contact(TENANT, OTHER.contact_id)

    result = await service.erase_contact(TENANT, CONTACT, actor="dpo@example.com", reason="art. 18")
    assert result.status == "erased" and result.reference == subject_ref(TENANT, CONTACT)
    assert result.counts["conversation_states"] == 1 and result.counts["turns"] >= 1

    assert await where_the_contact_still_appears(world) == {}  # not in ANY table, any column
    assert await service.locate_contact(TENANT, CONTACT) == await service.locate_contact(
        TENANT, "x"
    )
    assert await service.locate_contact(TENANT, OTHER.contact_id) == before_other  # others intact


async def test_what_other_guarantees_need_stays_as_a_content_free_tombstone(
    world: World, api: ApiHandle, service: RetentionService
) -> None:
    await full_conversation(world, api)
    await service.erase_contact(TENANT, CONTACT, actor="dpo", reason="request")
    pool = world.db.pool
    inbox = await pool.fetch("SELECT text, media, status, event_id FROM inbox_events")
    assert inbox and all(r["text"] == "" and r["status"] in ("CONSUMED", "DEAD") for r in inbox)
    outbox = await pool.fetch("SELECT text, status, idempotency_key FROM outbox_messages")
    assert outbox and all(r["text"] == "" and r["idempotency_key"] for r in outbox)
    ledger = await pool.fetch(
        "SELECT status, args_hash, request_json, result_json FROM tool_invocations"
    )
    assert ledger and all(
        r["status"] == "SUCCEEDED" and r["args_hash"] == "erased" and r["request_json"] == {}
        for r in ledger
    )  # the external effect is still on record; its content is not
    # a redelivery of an erased event is still recognised as a duplicate, never a new turn
    assert await world.inbox.insert_if_absent(event("e-1", "oi", clock=world.clock)) is False


async def test_the_erasure_is_audited_without_the_person_or_the_content(
    world: World, api: ApiHandle, service: RetentionService
) -> None:
    await full_conversation(world, api)
    await service.erase_contact(TENANT, CONTACT, actor="dpo@example.com", reason="art. 18")
    (record,) = await PostgresAuditLog(world.db).list(TENANT, action="contact.erase")
    assert (record.actor, record.outcome) == ("dpo@example.com", "ok")
    assert (
        record.subject_id == subject_ref(TENANT, CONTACT) and record.details["reason"] == "art. 18"
    )
    dumped = json.dumps(record.model_dump(mode="json"))
    assert CONTACT not in dumped and IDENTITY.conversation_id not in dumped
    assert record.details["turns"] >= 1 and record.details["conversations"] == 1


async def test_an_unknown_contact_is_reported_not_silently_ignored(
    service: RetentionService,
) -> None:
    result = await service.erase_contact(TENANT, "nobody", actor="dpo", reason="x")
    assert result.status == "nothing_found" and result.counts == {}


# --- what blocks an erasure ---


async def test_a_pending_confirmation_and_an_undelivered_prompt_block_until_forced(
    world: World, api: ApiHandle, service: RetentionService
) -> None:
    await propose(world, api)  # the prompt is still in the outbox, the action still pending
    result = await service.erase_contact(TENANT, CONTACT, actor="dpo", reason="x")
    assert result.status == "blocked"
    assert set(result.blockers) == {"message_awaiting_delivery", "confirmation_pending"}
    assert (await service.locate_contact(TENANT, CONTACT)).counts[
        "turns"
    ] >= 1  # nothing was touched
    (refused,) = await PostgresAuditLog(world.db).list(TENANT, action="contact.erase")
    assert refused.outcome == "refused" and set(refused.details["blockers"]) == set(result.blockers)

    forced = await service.erase_contact(TENANT, CONTACT, actor="dpo", reason="x", force=True)
    assert forced.status == "erased"  # undelivered messages and confirmations can be cancelled
    assert await where_the_contact_still_appears(world) == {}
    assert await world.db.pool.fetchval("SELECT count(*) FROM pending_actions") == 0


async def test_an_unfinished_external_effect_is_never_forced_away(
    world: World, api: ApiHandle, service: RetentionService
) -> None:
    await propose(world, api)
    await deliver_prompt(world)
    api.state.fault = {"status_after_effect": 503}  # the booking exists, the answer was lost
    await answer(world, api, "sim", FakeLLM([text_response("...")]), flows=True)
    api.state.fault = None
    status = await world.db.pool.fetchval(
        "SELECT status FROM tool_invocations WHERE capability = 'scheduling.create'"
    )
    assert status in ("UNKNOWN", "EXECUTING", "RECONCILING")
    result = await service.erase_contact(TENANT, CONTACT, actor="dpo", reason="x", force=True)
    assert result.status == "blocked" and "external_effect_unfinished" in result.blockers
    assert (await service.locate_contact(TENANT, CONTACT)).counts["tool_invocations"] >= 1


async def test_a_conversation_being_processed_cannot_be_erased(
    world: World, service: RetentionService
) -> None:
    await world.inbox.insert_if_absent(event("e-1", "oi", clock=world.clock))
    await world.coordinator("c", FakeLLM([text_response("Olá!")])).run_once()
    await world.outbox_worker("sender").run_once()
    lease = await world.leases.acquire(KEY, "a-turn-is-running", timedelta(seconds=30))
    assert lease is not None
    # the lease is stamped with the test's fixed clock (2026-10-05 08:00), the erasure check reads
    # the DATABASE clock: once real time passed that instant the lease would look expired
    await world.db.pool.execute(
        "UPDATE conversation_states SET lease_expires_at = clock_timestamp() + interval '30 s'"
    )
    result = await service.erase_contact(TENANT, CONTACT, actor="dpo", reason="x", force=True)
    assert result.status == "blocked" and "conversation_leased" in result.blockers
    await world.leases.release(lease)
    assert (
        await service.erase_contact(TENANT, CONTACT, actor="dpo", reason="x")
    ).status == "erased"


async def test_an_open_turn_blocks_the_erasure(world: World, service: RetentionService) -> None:
    await world.inbox.insert_if_absent(event("e-1", "oi", clock=world.clock))
    await world.coordinator("c", FakeLLM([text_response("Olá!")])).run_once()
    await world.outbox_worker("sender").run_once()
    await world.db.pool.execute("UPDATE turns SET status = 'PROCESSING', completed_at = NULL")
    result = await service.erase_contact(TENANT, CONTACT, actor="dpo", reason="x", force=True)
    assert result.status == "blocked" and "turn_in_progress" in result.blockers


# --- retention windows ---


async def seeded(world: World, api: ApiHandle) -> None:
    await full_conversation(world, api)
    await world.db.pool.execute(
        "UPDATE inbox_events SET media = '[{\"media_id\": \"m1\"}]'::jsonb WHERE event_id = 'e-1'"
    )


async def test_nothing_is_touched_inside_its_window(
    world: World, api: ApiHandle, service: RetentionService
) -> None:
    await seeded(world, api)
    result = await service.apply_retention(
        TENANT, actor="job", now=world.clock.now() + timedelta(days=1)
    )
    assert result.total == 0
    assert await world.db.pool.fetchval("SELECT count(*) FROM inbox_events WHERE text <> ''") >= 1


async def test_content_past_its_window_is_redacted_but_the_structure_stays(
    world: World, api: ApiHandle, service: RetentionService
) -> None:
    await seeded(world, api)
    far = world.clock.now() + timedelta(days=400)
    result = await service.apply_retention(TENANT, actor="job", now=far)
    assert all(
        result.counts[k] >= 1
        for k in ("journal_payloads", "inbound_messages", "outbound_messages", "turn_texts",
                  "media_references", "conversation_states")
    )  # fmt: skip
    pool = world.db.pool
    assert await pool.fetchval("SELECT count(*) FROM inbox_events WHERE text <> ''") == 0
    assert await pool.fetchval("SELECT count(*) FROM outbox_messages WHERE text <> ''") == 0
    assert await pool.fetchval("SELECT count(*) FROM turns WHERE user_text <> ''") == 0
    assert await pool.fetchval("SELECT count(*) FROM inbox_events WHERE media <> '[]'::jsonb") == 0
    journal = await pool.fetch("SELECT step_type, request_hash, payload FROM turn_journal")
    assert journal and all(r["payload"] == {"redacted": True} for r in journal)
    assert any(r["request_hash"] for r in journal)  # hashes and metadata survive (DESIGN 6)
    state = await pool.fetchrow("SELECT state_json, ownership, contact_id FROM conversation_states")
    assert state is not None and state["state_json"] == {} and state["ownership"] == "BOT"
    assert await pool.fetchval("SELECT count(*) FROM tool_invocations") >= 1  # the ledger is kept
    again = await service.apply_retention(TENANT, actor="job", now=far)
    assert again.total == 0  # idempotent


async def test_each_window_is_the_tenants_own(
    world: World, api: ApiHandle, service: RetentionService
) -> None:
    await seeded(world, api)
    await service.set_policy(TENANT, RetentionPolicy(message_days=1, media_reference_days=365,
        journal_payload_days=365, conversation_state_days=365), actor="admin")  # fmt: skip
    policy = await service.policy_for(TENANT)
    assert policy.message_days == 1 and (await service.policy_for("other")).message_days == 90
    result = await service.apply_retention(
        TENANT, actor="job", now=world.clock.now() + timedelta(days=3)
    )
    assert result.counts["inbound_messages"] >= 1 and result.counts["media_references"] == 0
    assert result.counts["journal_payloads"] == 0 and result.counts["conversation_states"] == 0
    with pytest.raises(ValidationError):
        RetentionPolicy(message_days=0)


async def test_a_conversation_with_work_open_or_with_a_person_keeps_its_state(
    world: World, api: ApiHandle, service: RetentionService
) -> None:
    await propose(world, api)  # a confirmation is pending
    far = world.clock.now() + timedelta(days=400)
    await service.apply_retention(TENANT, actor="job", now=far)
    state = await world.db.pool.fetchval("SELECT state_json FROM conversation_states")
    assert state != {}  # the pending action's conversation is not wiped under it
    await world.db.pool.execute("UPDATE pending_actions SET status = 'REJECTED'")
    await world.db.pool.execute("UPDATE conversation_states SET ownership = 'HUMAN'")
    await service.apply_retention(TENANT, actor="job", now=far)
    assert await world.db.pool.fetchval("SELECT state_json FROM conversation_states") != {}


async def test_a_dry_run_reports_real_counts_and_changes_nothing(
    world: World, api: ApiHandle, service: RetentionService
) -> None:
    await seeded(world, api)
    far = world.clock.now() + timedelta(days=400)
    dry = await service.apply_retention(TENANT, actor="job", now=far, dry_run=True)
    assert dry.dry_run and dry.total > 0
    assert await world.db.pool.fetchval("SELECT count(*) FROM inbox_events WHERE text <> ''") >= 1
    assert await PostgresAuditLog(world.db).list(TENANT, action="retention.apply") == []
    real = await service.apply_retention(TENANT, actor="job", now=far)
    assert real.counts == dry.counts  # what it said it would do is what it did
    (trail,) = await PostgresAuditLog(world.db).list(TENANT, action="retention.apply")
    assert (
        trail.actor == "job"
        and trail.details["inbound_messages"] == real.counts["inbound_messages"]
    )


async def test_changing_a_policy_is_audited(service: RetentionService, world: World) -> None:
    await service.set_policy(TENANT, RetentionPolicy(message_days=30), actor="admin@example.com")
    (trail,) = await PostgresAuditLog(world.db).list(TENANT, action="retention.policy")
    assert trail.actor == "admin@example.com" and trail.details["message_days"] == 30
