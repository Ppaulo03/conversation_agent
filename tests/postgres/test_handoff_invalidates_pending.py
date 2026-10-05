"""Phase 14 (audit): a handoff ends what the bot had proposed (INV-060).

Handing a conversation to a person must not leave a confirmation behind that a later "yes" could
use after the conversation returns to the bot; and asking for a person is never read as an answer
to an open confirmation.
"""

from __future__ import annotations

import pytest

from conftest import ApiHandle
from conversation_agent.adapters.clock import SystemClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response, tool_call_response
from conversation_agent.adapters.manifest.yaml_loader import load_manifest_file
from conversation_agent.adapters.postgres.coordination import CoordinationTime
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.lease import PostgresLeaseStore
from conversation_agent.adapters.postgres.uow import PostgresUnitOfWorkFactory
from conversation_agent.adapters.senders.console import ConsoleChannel
from conversation_agent.app.runtime import Runtime
from conversation_agent.core.compiler import compile_manifest
from conversation_agent.core.models.runtime import ConversationKey
from conversation_agent.engine.ownership import OwnershipService
from postgres.test_runtime_paths import identity, runtime_for
from vertical_slice.wiring import MANIFEST_PATH

CLOCK = SystemClock("America/Sao_Paulo")
BOOKING = {
    "service_id": "haircut",
    "start_at": "2026-10-06T10:00:00-03:00",
    "duration_minutes": 30,
}


def with_human_request():  # type: ignore[no-untyped-def]
    raw = load_manifest_file(MANIFEST_PATH)
    raw["human_request"] = {
        "triggers": ["falar com atendente", "falar com um atendente"],
        "reply": "Claro, vou chamar um atendente.",
    }
    return compile_manifest(raw)


async def propose(
    db: PostgresDatabase, api: ApiHandle, llm: FakeLLM
) -> tuple[Runtime, ConsoleChannel, list[str]]:
    out: list[str] = []
    channel = ConsoleChannel(identity("h"), CLOCK, write=out.append)
    runtime = runtime_for(db, api, llm, channel, CLOCK, with_human_request())
    await runtime.receive(channel.inbound("quero terça às 10h"))
    await runtime.drain()
    assert "SIM" in out[-1]  # a confirmation is open
    return runtime, channel, out


async def status_of_action(db: PostgresDatabase) -> str:
    return str(await db.pool.fetchval("SELECT status FROM pending_actions"))


def operators(db: PostgresDatabase) -> OwnershipService:
    coordination = CoordinationTime(db)
    return OwnershipService(
        PostgresLeaseStore(db, CLOCK, coordination),
        PostgresUnitOfWorkFactory(db, CLOCK, coordination),
        owner="ops",
    )


KEY = ConversationKey(tenant_id="tenant-1", conversation_id="conv-h")


def proposing_llm() -> FakeLLM:
    return FakeLLM(
        [tool_call_response("scheduling__create", BOOKING), text_response("Posso reservar.")]
    )


async def test_asking_for_a_person_beats_an_open_confirmation_and_ends_it(
    db: PostgresDatabase, api: ApiHandle
) -> None:
    runtime, channel, out = await propose(db, api, proposing_llm())
    await runtime.receive(channel.inbound("quero falar com um atendente"))
    await runtime.drain()

    assert out[-1] == "bot> Claro, vou chamar um atendente."  # not a "did not understand"
    assert await db.pool.fetchval("SELECT ownership FROM conversation_states") == "HANDOFF_PENDING"
    assert await status_of_action(db) == "INVALIDATED"
    assert (
        api.state.bookings == {}
        and await db.pool.fetchval("SELECT count(*) FROM tool_invocations") == 0
    )


async def test_a_yes_after_the_conversation_comes_back_never_runs_the_old_action(
    db: PostgresDatabase, api: ApiHandle
) -> None:
    llm = proposing_llm()
    runtime, channel, _ = await propose(db, api, llm)
    await runtime.receive(channel.inbound("quero falar com um atendente"))
    await runtime.drain()

    ops = operators(db)
    await ops.assign_human(KEY)
    await ops.return_to_bot(KEY)  # well before the confirmation's own expiry
    llm._script.append(text_response("Em que posso ajudar?"))  # the bot answers as a normal turn
    await runtime.receive(channel.inbound("sim"))
    await runtime.drain()

    assert api.state.bookings == {}  # the proposal made BEFORE the person is gone
    assert await status_of_action(db) == "INVALIDATED"
    assert await db.pool.fetchval("SELECT count(*) FROM tool_invocations") == 0


@pytest.mark.parametrize("move", ["assign_human", "request_handoff"])
async def test_any_move_away_from_the_bot_invalidates_what_was_pending(
    db: PostgresDatabase, api: ApiHandle, move: str
) -> None:  # an operator acting directly, not only the bot's own handoff
    await propose(db, api, proposing_llm())
    await getattr(operators(db), move)(KEY)
    assert await status_of_action(db) == "INVALIDATED"


async def test_an_ended_action_stays_ended_when_the_conversation_returns(
    db: PostgresDatabase, api: ApiHandle
) -> None:
    await propose(db, api, proposing_llm())
    ops = operators(db)
    await ops.assign_human(KEY)
    await ops.return_to_bot(KEY)
    assert await status_of_action(db) == "INVALIDATED"  # once ended it stays ended
