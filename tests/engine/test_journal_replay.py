"""Turn Journal replay: INV-014, INV-017, INV-018 (Phase 1: in-memory journal)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import ApiHandle
from conversation_agent.adapters.journal.memory import InMemoryTurnJournal
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.llm.replay import ReplayLLM
from conversation_agent.core.errors import (
    JournalConflictError,
    JournalDivergenceError,
    LLMProviderError,
)
from conversation_agent.core.models.conversation import ConversationState
from conversation_agent.core.models.journal import JournalEntry, JournalStepType
from support.builders import IDENTITY, NOW, availability_call, new_clock, new_journal
from vertical_slice.wiring import build_engine

TURN = "turn-replay-1"


def make_engine(llm, api: ApiHandle, journal=None, clock=None):  # type: ignore[no-untyped-def]
    engine, _, _ = build_engine(
        llm,
        api_base_url=api.base_url,
        journal=journal if journal is not None else new_journal(),
        clock=clock or new_clock(),
    )
    return engine


async def test_replay_reuses_llm_response_and_tool_result_after_a_crash(api: ApiHandle) -> None:
    """C10-style (in-memory): crash after step 1; the rerun repeats neither LLM nor tool."""
    journal = new_journal()
    crashing = FakeLLM(
        [availability_call("haircut", "2026-10-06"), LLMProviderError("provider died")]
    )
    with pytest.raises(LLMProviderError):
        await make_engine(crashing, api, journal).process_turn(
            IDENTITY, ConversationState(), "quero terça", TURN
        )
    assert crashing.calls == 2 and len(api.availability_requests()) == 1

    resumed = FakeLLM([text_response("Tenho horários na terça.")])  # only step 2 is new
    outcome = await make_engine(resumed, api, journal).process_turn(
        IDENTITY, ConversationState(), "quero terça", TURN
    )
    assert resumed.calls == 1  # LLM step 1 came from the journal
    assert len(api.availability_requests()) == 1  # tool result came from the journal
    assert outcome.reply == "Tenho horários na terça."
    types = [e.step_type for e in await journal.entries(TURN)]
    assert types.count(JournalStepType.LLM_RESPONSE) == 2
    assert (
        types[0] is JournalStepType.INBOUND_AGGREGATED
        and types[-1] is JournalStepType.TURN_COMPLETED
    )


async def test_replaying_a_completed_turn_calls_nothing(api: ApiHandle) -> None:
    journal = new_journal()
    llm = FakeLLM([availability_call("haircut", "2026-10-06"), text_response("Horários: 09:00.")])
    first = await make_engine(llm, api, journal).process_turn(
        IDENTITY, ConversationState(), "quero terça", TURN
    )
    silent = FakeLLM([])  # any call would raise "script exhausted"
    again = await make_engine(silent, api, journal).process_turn(
        IDENTITY, ConversationState(), "quero terça", TURN
    )
    assert silent.calls == 0 and len(api.availability_requests()) == 1
    assert again.reply == first.reply and again.state == first.state


async def test_llm_request_is_journaled_before_the_call_with_a_stable_id(api: ApiHandle) -> None:
    journal = new_journal()
    with pytest.raises(LLMProviderError):
        await make_engine(FakeLLM([LLMProviderError("x")]), api, journal).process_turn(
            IDENTITY, ConversationState(), "oi", TURN
        )
    entries = await journal.entries(TURN)
    assert [e.step_type for e in entries] == [
        JournalStepType.INBOUND_AGGREGATED,
        JournalStepType.LLM_REQUEST,  # persisted although the call failed
    ]
    request_id = entries[1].payload["llm_request_id"]

    seen = FakeLLM([text_response("ok")])
    await make_engine(seen, api, journal).process_turn(IDENTITY, ConversationState(), "oi", TURN)
    assert seen.requests[0].request_id == request_id  # retry reuses the same id


async def test_journal_request_hash_divergence_fails_closed(api: ApiHandle) -> None:  # INV-017
    journal = new_journal()
    await make_engine(FakeLLM([text_response("oi")]), api, journal).process_turn(
        IDENTITY, ConversationState(), "mensagem A", TURN
    )
    llm = FakeLLM([text_response("não deve ser chamado")])
    with pytest.raises(JournalDivergenceError):
        await make_engine(llm, api, journal).process_turn(
            IDENTITY, ConversationState(), "mensagem B", TURN
        )
    assert llm.calls == 0 and api.requests == []


async def test_journal_step_type_divergence_fails_closed(api: ApiHandle) -> None:  # INV-017
    journal = InMemoryTurnJournal()
    await make_engine(FakeLLM([text_response("oi")]), api, journal).process_turn(
        IDENTITY, ConversationState(), "oi", TURN
    )
    stored = await journal.get(TURN, 1)
    assert stored is not None
    journal._entries[(TURN, 1)] = stored.model_copy(
        update={"step_type": JournalStepType.TOOL_RESULT}
    )
    llm = FakeLLM([text_response("x")])
    with pytest.raises(JournalDivergenceError):
        await make_engine(llm, api, journal).process_turn(IDENTITY, ConversationState(), "oi", TURN)
    assert llm.calls == 0


async def test_changed_llm_request_content_on_replay_fails_closed(api: ApiHandle) -> None:
    """Same inbound text but different conversation history -> different LLM request hash."""
    journal = new_journal()
    first = await make_engine(FakeLLM([text_response("a")]), api, journal).process_turn(
        IDENTITY, ConversationState(), "oi", "t-a"
    )
    # Re-running turn t-a on top of a *different* prior state must not silently reuse the answer.
    with pytest.raises(JournalDivergenceError):
        await make_engine(FakeLLM([text_response("b")]), api, journal).process_turn(
            IDENTITY, first.state, "oi", "t-a"
        )


async def test_replay_reuses_turn_reference_time(api: ApiHandle) -> None:  # INV-018
    journal = new_journal()
    clock = new_clock()
    first = FakeLLM([availability_call("haircut", "2026-10-06"), LLMProviderError("crash")])
    with pytest.raises(LLMProviderError):
        await make_engine(first, api, journal, clock).process_turn(
            IDENTITY, ConversationState(), "amanhã", TURN
        )
    clock.set(NOW + timedelta(days=1, hours=3))  # "now" moved on before the replay
    second = FakeLLM([text_response("ok")])
    await make_engine(second, api, journal, clock).process_turn(
        IDENTITY, ConversationState(), "amanhã", TURN
    )
    assert "2026-10-05T08:00:00-03:00" in second.requests[0].system
    assert second.requests[0].system == first.requests[0].system  # identical "now" in the prompt


async def test_replay_llm_serves_a_cassette_derived_from_the_journal(api: ApiHandle) -> None:
    journal = new_journal()
    llm = FakeLLM([availability_call("haircut", "2026-10-06"), text_response("Tenho 09:00.")])
    original = await make_engine(llm, api, journal).process_turn(
        IDENTITY, ConversationState(), "quero terça", TURN
    )
    cassette = ReplayLLM.from_journal(await journal.entries(TURN))
    fresh = await make_engine(cassette, api).process_turn(
        IDENTITY, ConversationState(), "quero terça", TURN
    )
    assert fresh.reply == original.reply == "Tenho 09:00."
    assert cassette.calls == 2


async def test_replay_llm_fails_on_unrecorded_request(api: ApiHandle) -> None:
    with pytest.raises(LLMProviderError):
        await make_engine(ReplayLLM({}), api).process_turn(
            IDENTITY, ConversationState(), "oi", TURN
        )


async def test_journal_append_is_conflict_checked() -> None:
    journal = InMemoryTurnJournal()
    entry = JournalEntry(turn_id="t", step_index=0, step_type=JournalStepType.INBOUND_AGGREGATED)
    await journal.append(entry)
    with pytest.raises(JournalConflictError):
        await journal.append(entry)
