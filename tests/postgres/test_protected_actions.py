"""Phase 3: protected actions with explicit, action-scoped confirmation (real PostgreSQL + the
reference API). INV-004, INV-005, INV-010, INV-015, INV-019, INV-022; chaos C01, C02, C03, C13.

Flow under test: the model proposes `scheduling.create` -> PolicyGate says REQUIRE_CONFIRMATION
-> a PendingAction + its prompt (outbox, same transaction) -> only a later inbound that is an
eligible reply to an ACCEPTED prompt can confirm -> ONE transaction writes the confirmation,
CONFIRMED and the PREPARED invocation -> the ledger executes -> the user is told the outcome.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from conftest import ApiHandle
from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.faults import ChaosFaults, SimulatedCrash
from conversation_agent.adapters.llm.fake import FakeLLM, text_response, tool_call_response
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.canonical import stable_hash
from conversation_agent.core.errors import LLMProviderError
from conversation_agent.core.models.llm import LLMResponse, LLMStopReason, LLMUsage
from postgres.world import KEY, TTL, World, event

BOOKING = {
    "service_id": "haircut",
    "start_at": "2026-10-06T10:00:00-03:00",
    "duration_minutes": 30,
}
BOOKING_16H = {**BOOKING, "start_at": "2026-10-06T16:00:00-03:00"}


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


def posts(api: ApiHandle) -> list[dict[str, object]]:
    return [r for r in api.requests if r["method"] == "POST"]


def coord(world: World, api: ApiHandle, owner: str, llm: Any, **kw: Any):  # type: ignore[no-untyped-def]
    return world.coordinator(
        owner, llm, providers={"http": world.http_provider(api.base_url)}, **kw
    )


def structured(decision: str, confidence: float) -> LLMResponse:
    return LLMResponse(
        parts=(),
        stop_reason=LLMStopReason.END_TURN,
        usage=LLMUsage(input_tokens=1, output_tokens=1),
        structured={"decision": decision, "confidence": confidence},
    )


async def propose(world: World, api: ApiHandle, args: dict[str, Any] = BOOKING) -> None:
    """Turn 1: the user asks, the model proposes; a PendingAction + prompt are created."""
    await world.inbox.insert_if_absent(
        event(f"e-{world.clock.now().isoformat()}", "quero terça às 10h", clock=world.clock)
    )
    llm = FakeLLM(
        [tool_call_response("scheduling__create", args), text_response("Posso reservar o horário.")]
    )
    run = await coord(world, api, "proposer", llm).process_conversation(KEY)
    assert run.status == "done"


async def deliver_prompt(world: World) -> None:
    """The channel accepts the prompt (ACCEPTED, with a provider message id and time)."""
    assert await world.outbox_worker("sender").run_once() >= 1


async def answer(
    world: World,
    api: ApiHandle,
    text: str,
    llm: Any,
    *,
    after_s: float = 60,
    skew_s: float | None = None,
    reply_to: str | None = None,
    owner: str = "answerer",
    **kw: Any,
):  # type: ignore[no-untyped-def]
    """The user's reply arrives `after_s` after the prompt (provider clock)."""
    world.clock.set(world.clock.now() + timedelta(seconds=after_s))
    occurred = world.clock.now() + timedelta(seconds=skew_s or 0)
    await world.inbox.insert_if_absent(
        event(
            f"a-{world.clock.now().isoformat()}-{text[:8]}",
            text,
            clock=world.clock,
            provider_occurred_at=occurred,
            reply_to=reply_to,
        )
    )
    return await coord(world, api, owner, llm, **kw).process_conversation(KEY)


async def pending(world: World) -> dict[str, Any]:
    row = await world.db.pool.fetchrow("SELECT * FROM pending_actions ORDER BY created_at DESC")
    assert row is not None
    return dict(row)


async def statuses(world: World) -> list[str]:
    rows = await world.db.pool.fetch("SELECT status FROM pending_actions ORDER BY created_at")
    return [r["status"] for r in rows]


# --- the proposal ---


async def test_protected_action_has_persistent_action_id_and_scoped_confirmation(
    world: World, api: ApiHandle
) -> None:  # INV-004
    await propose(world, api)
    action = await pending(world)
    prompt = await world.db.pool.fetchrow("SELECT * FROM outbox_messages")
    assert prompt is not None
    assert len(action["action_id"]) == 32 and action["status"] == "PENDING_CONFIRMATION"
    assert prompt["action_id"] == action["action_id"]  # the prompt row names its action
    assert action["latest_prompt_outbox_id"] == prompt["outbox_id"]
    assert "Posso confirmar?" in prompt["text"] and "Agendar haircut" in prompt["text"]
    assert await world.count("tool_invocations") == 0 and posts(api) == []  # nothing executes

    await deliver_prompt(world)
    run = await answer(world, api, "Sim", FakeLLM([text_response("Agendado para terça às 10h!")]))
    assert run.status == "done"

    inv = await world.ledger.get(
        KEY.tenant_id, await world.db.pool.fetchval("SELECT invocation_id FROM tool_invocations")
    )
    assert inv is not None and inv.action_id == action["action_id"]  # scoped to THIS action
    assert (
        inv.invocation_id
        == stable_hash(KEY.tenant_id, KEY.conversation_id, action["action_id"])[:32]
    )
    assert (
        inv.idempotency_key == inv.invocation_id
        and inv.intent.tool_name == "erp_create_reservation"
    )
    assert (await pending(world))["status"] == "CONFIRMED"
    (confirmation,) = await world.db.pool.fetch("SELECT * FROM action_confirmations")
    assert (confirmation["decision"], confirmation["interpreter"]) == ("confirm", "rule")
    assert confirmation["prompt_outbox_id"] == prompt["outbox_id"]
    assert len(posts(api)) == 1 and len(api.state.bookings) == 1


async def test_the_runtime_owns_the_confirmation_question_not_the_model(
    world: World, api: ApiHandle
) -> None:
    await propose(world, api)
    text = await world.db.pool.fetchval("SELECT text FROM outbox_messages")
    assert text.startswith("Posso reservar o horário.")  # the model's summary...
    assert text.rstrip().endswith("Responda SIM para confirmar ou NÃO para cancelar.")  # ...+ ours


# --- eligibility (INV-022) ---


async def test_a_yes_before_the_prompt_was_accepted_never_confirms(
    world: World, api: ApiHandle
) -> None:
    await propose(world, api)  # the prompt stays PENDING in the outbox: not ACCEPTED
    run = await answer(world, api, "sim", FakeLLM([]))
    assert run.status == "done"
    assert posts(api) == [] and await world.count("tool_invocations") == 0
    assert (await pending(world))["status"] == "PENDING_CONFIRMATION"
    assert (await pending(world))["confirmation_attempts"] == 1  # it asked again instead


@pytest.mark.parametrize("status", ["SENDING", "QUEUED", "UNKNOWN"])
async def test_C13_confirm_while_prompt_ambiguous(
    world: World, api: ApiHandle, status: str
) -> None:
    await propose(world, api)
    old = await world.db.pool.fetchval("SELECT outbox_id FROM outbox_messages")
    await world.db.pool.execute("UPDATE outbox_messages SET status=$1", status)

    await answer(world, api, "sim", FakeLLM([]))
    assert posts(api) == [] and await world.count("tool_invocations") == 0  # nothing executes
    action = await pending(world)
    assert action["status"] == "PENDING_CONFIRMATION" and action["confirmation_attempts"] == 1
    assert action["latest_prompt_outbox_id"] != old  # a NEW prompt row, a new identity
    rows = await world.db.pool.fetch(
        "SELECT outbox_id, status, text FROM outbox_messages ORDER BY created_at, message_index"
    )
    assert len(rows) == 2 and "Não consegui confirmar que o seu" in rows[1]["text"]
    if status == "UNKNOWN":
        assert rows[0]["status"] == "SUPERSEDED"  # the doubtful prompt can no longer authorise


async def test_a_yes_that_predates_the_prompt_is_not_an_answer_to_it(
    world: World, api: ApiHandle
) -> None:
    await propose(world, api)
    await deliver_prompt(world)
    llm = FakeLLM([text_response("Pois não?")])  # treated as an ordinary message
    await answer(world, api, "sim", llm, after_s=60, skew_s=-120)  # occurred 1 min BEFORE accept
    assert posts(api) == [] and await world.count("tool_invocations") == 0
    assert (await pending(world))["status"] == "PENDING_CONFIRMATION"
    assert llm.calls == 1  # it went to the normal loop, it did not confirm anything


async def test_inside_the_skew_window_the_runtime_asks_again(world: World, api: ApiHandle) -> None:
    await propose(world, api)
    await deliver_prompt(world)
    await answer(world, api, "sim", FakeLLM([]), after_s=0, skew_s=1)  # +1s: inside tolerance
    assert posts(api) == []
    assert (await pending(world))["confirmation_attempts"] == 1


async def test_an_explicit_reply_to_the_prompt_resolves_the_skew_ambiguity(
    world: World, api: ApiHandle
) -> None:
    await propose(world, api)
    await deliver_prompt(world)
    provider_id = await world.db.pool.fetchval("SELECT provider_message_id FROM outbox_messages")
    llm = FakeLLM([text_response("Agendado!")])
    await answer(world, api, "sim", llm, after_s=0, skew_s=1, reply_to=provider_id)
    assert len(posts(api)) == 1 and (await pending(world))["status"] == "CONFIRMED"


async def test_a_reply_to_some_other_message_does_not_confirm(world: World, api: ApiHandle) -> None:
    await propose(world, api)
    await deliver_prompt(world)
    llm = FakeLLM([text_response("Certo.")])
    await answer(world, api, "sim", llm, reply_to="some-other-message")
    assert posts(api) == [] and (await pending(world))["status"] == "PENDING_CONFIRMATION"


# --- decisions ---


async def test_changed_protected_args_invalidate_confirmation(
    world: World, api: ApiHandle
) -> None:  # INV-010
    await propose(world, api)
    await deliver_prompt(world)
    first = await pending(world)

    # "Sim, mas às 16h" must NOT confirm the 10h booking: it invalidates it and re-plans.
    llm = FakeLLM(
        [tool_call_response("scheduling__create", BOOKING_16H), text_response("Podemos às 16h.")]
    )
    await answer(world, api, "Sim, mas às 16h", llm)
    assert posts(api) == []  # nothing executed on the strength of that "sim"
    assert await statuses(world) == ["INVALIDATED", "PENDING_CONFIRMATION"]
    second = await pending(world)
    assert second["action_id"] != first["action_id"] and second["args_hash"] != first["args_hash"]
    assert second["request_json"]["args"]["start_at"] == "2026-10-06T19:00:00+00:00"

    await deliver_prompt(world)  # the NEW prompt is accepted; only now can the user confirm
    await answer(world, api, "sim", FakeLLM([text_response("Agendado às 16h!")]))
    (booking,) = api.state.bookings.values()
    assert booking["start"].hour == 16  # the confirmed action is the NEW one
    assert await statuses(world) == ["INVALIDATED", "CONFIRMED"]


async def test_a_refusal_rejects_the_action_and_nothing_executes(
    world: World, api: ApiHandle
) -> None:
    await propose(world, api)
    await deliver_prompt(world)
    await answer(world, api, "Não", FakeLLM([]))
    assert posts(api) == [] and (await pending(world))["status"] == "REJECTED"
    last = await world.db.pool.fetchval(
        "SELECT text FROM outbox_messages ORDER BY created_at DESC, message_index DESC LIMIT 1"
    )
    assert last == "Tudo bem, cancelei o pedido."


async def test_a_second_yes_never_executes_twice(world: World, api: ApiHandle) -> None:
    await propose(world, api)
    await deliver_prompt(world)
    await answer(world, api, "sim", FakeLLM([text_response("Agendado!")]))
    await deliver_prompt(world)
    await answer(world, api, "sim", FakeLLM([text_response("Já está agendado.")]), after_s=5)
    assert len(posts(api)) == 1 and await world.count("tool_invocations") == 1


async def test_an_expired_action_cannot_be_confirmed(world: World, api: ApiHandle) -> None:
    await propose(world, api)
    await deliver_prompt(world)
    await answer(world, api, "sim", FakeLLM([]), after_s=31 * 60)  # ttl is 30 minutes
    assert posts(api) == [] and (await pending(world))["status"] == "EXPIRED"


async def test_unclear_replies_are_bounded_then_the_action_is_dropped(
    world: World, api: ApiHandle
) -> None:
    await propose(world, api)
    await deliver_prompt(world)
    for _ in range(2):  # two re-prompts are allowed (max_reprompts=2)...
        await answer(
            world, api, "hmm", FakeLLM([structured("unclear", 0.2)]), max_reprompts=2, after_s=10
        )
        await deliver_prompt(world)
    assert (await pending(world))["status"] == "PENDING_CONFIRMATION"
    await answer(
        world, api, "hmm", FakeLLM([structured("unclear", 0.2)]), max_reprompts=2, after_s=10
    )
    assert (await pending(world))["status"] == "EXPIRED"  # ...the third gives up
    assert posts(api) == []


async def test_the_llm_fallback_confirms_only_with_high_confidence_and_an_eligible_prompt(
    world: World, api: ApiHandle
) -> None:
    await propose(world, api)
    await deliver_prompt(world)
    await answer(
        world, api, "pode ser sim por gentileza", FakeLLM([structured("confirm", 0.6)]), after_s=10
    )
    assert posts(api) == [] and (await pending(world))["confirmation_attempts"] == 1  # too unsure
    await deliver_prompt(world)
    await answer(
        world,
        api,
        "claro, pode seguir em frente",
        FakeLLM([structured("garbage", 1.0)]),
        after_s=10,
    )
    assert posts(api) == []  # malformed model output is never a confirmation
    await deliver_prompt(world)
    llm = FakeLLM([structured("confirm", 0.95), text_response("Agendado!")])
    await answer(world, api, "perfeito, fecha esse horário pra mim", llm, after_s=10)
    assert len(posts(api)) == 1 and (await pending(world))["status"] == "CONFIRMED"
    (row,) = await world.db.pool.fetch(
        "SELECT interpreter FROM action_confirmations WHERE decision='confirm'"
    )
    assert row["interpreter"] == "llm"


async def test_human_owner_conversation_never_confirms_or_executes(
    world: World, api: ApiHandle
) -> None:  # INV-019
    await propose(world, api)
    await deliver_prompt(world)
    await world.db.pool.execute("UPDATE conversation_states SET ownership='HUMAN'")
    await answer(world, api, "sim", FakeLLM([]))
    assert posts(api) == [] and (await pending(world))["status"] == "PENDING_CONFIRMATION"


# --- chaos: the confirmation transaction ---


async def confirm_with_crash(world: World, api: ApiHandle, point: str) -> None:
    await propose(world, api)
    await deliver_prompt(world)
    with pytest.raises(SimulatedCrash):
        await answer(
            world, api, "sim", FakeLLM([text_response("Agendado!")]), faults=ChaosFaults(point)
        )
    world.clock.set(
        world.clock.now() + TTL + timedelta(seconds=1)
    )  # the crashed worker's lease ends


async def recover(world: World, api: ApiHandle) -> None:
    run = await coord(
        world, api, "recoverer", FakeLLM([text_response("Agendado!")])
    ).process_conversation(KEY)
    assert run.status == "done"


async def test_C01_before_confirm_commit_the_action_stays_pending_and_recovers(
    world: World, api: ApiHandle
) -> None:
    await confirm_with_crash(world, api, "C01_before_confirm_commit")
    assert (await pending(world))["status"] == "PENDING_CONFIRMATION"
    assert (
        await world.count("action_confirmations") == 0
        and await world.count("tool_invocations") == 0
    )
    await recover(world, api)  # the open turn is resumed: it confirms and executes, once
    assert (await pending(world))["status"] == "CONFIRMED" and len(posts(api)) == 1


async def test_C02_inside_confirm_prepare_tx_rolls_back_never_an_orphan_confirmed(
    world: World, api: ApiHandle
) -> None:  # INV-005
    await confirm_with_crash(world, api, "C02_inside_confirm_prepare_tx")
    # the confirmation row was written inside the transaction, then the process died: all gone
    assert await world.count("action_confirmations") == 0
    assert await world.count("pending_actions", "status='CONFIRMED'") == 0
    assert await world.count("tool_invocations") == 0 and posts(api) == []
    assert (await pending(world))["status"] == "PENDING_CONFIRMATION"
    await recover(world, api)
    assert len(posts(api)) == 1


async def test_C03_after_confirm_prepare_commit_recovery_finds_the_same_invocation(
    world: World, api: ApiHandle
) -> None:
    await confirm_with_crash(world, api, "C03_after_confirm_prepare_commit")
    action = await pending(world)
    assert action["status"] == "CONFIRMED"  # CONFIRMED and PREPARED committed together:
    inv_id = await world.db.pool.fetchval("SELECT invocation_id FROM tool_invocations")
    assert await world.count("tool_invocations", "status='PREPARED'") == 1
    assert posts(api) == []

    await recover(world, api)
    assert await world.db.pool.fetchval("SELECT invocation_id FROM tool_invocations") == inv_id
    assert await world.count("tool_invocations") == 1 and len(posts(api)) == 1  # same one, once
    assert (await world.ledger.get(KEY.tenant_id, inv_id)).status.value == "SUCCEEDED"  # type: ignore[union-attr]


async def test_confirmed_never_exists_without_a_prepared_invocation(
    world: World, api: ApiHandle
) -> None:
    for point in ("C02_inside_confirm_prepare_tx", "C03_after_confirm_prepare_commit"):
        await world.db.pool.execute(
            "TRUNCATE conversation_states, turns, turn_journal, inbox_events, outbox_messages, "
            "tool_invocations, pending_actions, action_confirmations CASCADE"
        )
        api.state.bookings.clear()
        api.state.by_key.clear()
        await confirm_with_crash(world, api, point)
        orphans = await world.count(
            "pending_actions p",
            "p.status='CONFIRMED' AND NOT EXISTS (SELECT 1 FROM tool_invocations i "
            "WHERE i.tenant_id=p.tenant_id AND i.action_id=p.action_id)",
        )
        assert orphans == 0, point


# --- outcomes ---


async def test_a_failed_attempt_needs_a_new_action_never_a_retry_of_the_same(
    world: World, api: ApiHandle
) -> None:  # INV-015
    await propose(world, api)
    await deliver_prompt(world)
    api.state.taken_slots = {
        __import__("datetime").datetime(
            2026, 10, 6, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("America/Sao_Paulo")
        )
    }
    await answer(world, api, "sim", FakeLLM([text_response("Esse horário acabou de ser ocupado.")]))
    first_inv = await world.db.pool.fetchrow(
        "SELECT invocation_id, action_id, status FROM tool_invocations"
    )
    assert first_inv is not None and first_inv["status"] == "FAILED"

    api.state.taken_slots = set()  # the user insists: a NEW semantic attempt
    await world.inbox.insert_if_absent(
        event("again", "tenta de novo", clock=world.clock, provider_occurred_at=world.clock.now())
    )
    llm = FakeLLM(
        [tool_call_response("scheduling__create", BOOKING), text_response("Posso tentar de novo.")]
    )
    world.clock.set(world.clock.now() + timedelta(seconds=5))
    await coord(world, api, "w3", llm).process_conversation(KEY)
    second = await pending(world)
    assert second["action_id"] != first_inv["action_id"]  # a brand-new action_id
    assert await world.count("tool_invocations") == 1  # nothing executed yet for it
    assert (
        await world.db.pool.fetchval("SELECT status FROM tool_invocations") == "FAILED"
    )  # untouched


async def test_ambiguous_write_in_a_confirmed_action_becomes_unknown_then_reconciles(
    world: World, api: ApiHandle
) -> None:
    await propose(world, api)
    await deliver_prompt(world)
    api.state.fault = {"status_after_effect": 503}  # booking created, answer lost
    run = await answer(world, api, "sim", FakeLLM([]))
    assert run.status == "waiting"  # outcome unknown: wait, do not retry blindly
    assert await world.db.pool.fetchval("SELECT status FROM tool_invocations") == "UNKNOWN"
    assert len(posts(api)) == 1 and len(api.state.bookings) == 1

    api.state.fault = None
    resolved = await world.reconciler(
        "rec", providers={"http": world.http_provider(api.base_url)}
    ).run_once()
    assert [i.status.value for i in resolved] == ["RECONCILED"]
    run = await coord(
        world, api, "finisher", FakeLLM([text_response("Agendado!")])
    ).process_conversation(KEY)
    assert run.status == "done" and len(posts(api)) == 1  # still exactly one booking request


async def test_when_the_llm_dies_after_the_side_effect_the_user_still_gets_a_deterministic_answer(
    world: World, api: ApiHandle
) -> None:
    await propose(world, api)
    await deliver_prompt(world)
    run = await answer(world, api, "sim", FakeLLM([LLMProviderError("down")]))
    assert run.status == "done" and len(posts(api)) == 1  # executed, and the turn completed
    last = await world.db.pool.fetchval(
        "SELECT text FROM outbox_messages ORDER BY created_at DESC, message_index DESC LIMIT 1"
    )
    assert last == "Seu pedido foi processado (situação: success)."


# --- why a yes did not count (the contact is told, not just "I did not understand") ---


async def last_reply(world: World) -> str:
    return str(
        await world.db.pool.fetchval(
            "SELECT text FROM outbox_messages ORDER BY created_at DESC, message_index DESC LIMIT 1"
        )
    )


async def test_a_yes_the_runtime_cannot_tie_to_the_prompt_is_told_why(
    world: World, api: ApiHandle
) -> None:
    await propose(world, api)
    await deliver_prompt(world)
    await answer(world, api, "sim", FakeLLM([]), after_s=1)  # inside the clock tolerance
    assert posts(api) == [] and (await pending(world))["status"] == "PENDING_CONFIRMATION"
    reply = await last_reply(world)
    assert "Não consegui confirmar que o seu" in reply and "Não entendi" not in reply


async def test_a_message_that_is_not_a_yes_still_gets_the_plain_reprompt(
    world: World, api: ApiHandle
) -> None:
    await propose(world, api)
    await deliver_prompt(world)
    await answer(world, api, "talvez", FakeLLM([structured("unclear", 0.9)]))
    assert "Não entendi" in await last_reply(world)  # nothing was provable OR said: just unclear
