"""Contact memory in the turn (INV-082): the profile in the prompt as quoted data, three tools whose
write is a journaled step, identity from the turn, and nothing at all for an agent without the
block."""

from __future__ import annotations

from typing import Any

from conftest import ApiHandle
from conversation_agent.adapters.contact_memory.memory import InMemoryContactMemory
from conversation_agent.adapters.llm.fake import FakeLLM, text_response, tool_call_response
from conversation_agent.core.definitions.agent import AgentDefinition
from conversation_agent.core.definitions.memory import MemorySettings
from conversation_agent.core.definitions.tool_selection import ToolRule, ToolSelectionSettings
from conversation_agent.core.models.conversation import ConversationState
from conversation_agent.core.models.journal import JournalStepType
from conversation_agent.core.models.llm import ToolResultPart
from conversation_agent.ports.contact_memory import MemoryLimits
from support.builders import IDENTITY, new_clock, new_journal
from vertical_slice.definitions import build_agent
from vertical_slice.wiring import build_engine

REMEMBER, SET_NAME, FORGET = "memory__remember", "memory__set_name", "memory__forget"
T, C = IDENTITY.tenant_id, IDENTITY.contact_id


def agent_with(memory: MemorySettings | None, **over: Any) -> AgentDefinition:
    agent = build_agent()
    fields = {n: getattr(agent, n) for n in AgentDefinition.model_fields}
    return AgentDefinition(**{**fields, "memory": memory, **over})


def engine_for(
    llm: FakeLLM, api: ApiHandle, memory: MemorySettings | None, store=None, journal=None, **over
):  # type: ignore[no-untyped-def]
    engine, _, _ = build_engine(
        llm,
        api_base_url=api.base_url,
        journal=journal if journal is not None else new_journal(),
        clock=new_clock(),
        agent=agent_with(memory, **over),
        contact_memory=store,
    )
    return engine


def tool_results(llm: FakeLLM, call: int) -> list[ToolResultPart]:
    return [
        p for m in llm.requests[call].messages for p in m.parts if isinstance(p, ToolResultPart)
    ]


async def test_without_the_block_nothing_is_loaded_offered_or_journaled(api: ApiHandle) -> None:
    llm, journal = FakeLLM([text_response("oi")]), new_journal()
    await engine_for(llm, api, None, journal=journal).process_turn(
        IDENTITY, ConversationState(), "oi", "t1"
    )
    assert not {t.name for t in llm.requests[0].tools} & {REMEMBER, SET_NAME, FORGET}
    assert "Contact memory" not in llm.requests[0].system
    kinds = {e.step_type for e in await journal.entries("t1")}
    assert (
        JournalStepType.CONTACT_PROFILE not in kinds and JournalStepType.MEMORY_WRITE not in kinds
    )


async def test_with_the_block_the_prompt_says_what_is_known_and_the_tools_are_offered(
    api: ApiHandle,
) -> None:
    store = InMemoryContactMemory()
    limits = MemoryLimits()
    await store.set_name(T, C, "Ana", limits)
    await store.remember(T, C, "Treina de manhã", limits)
    llm = FakeLLM([text_response("oi")])
    await engine_for(llm, api, MemorySettings(), store).process_turn(
        IDENTITY, ConversationState(), "oi", "t1"
    )
    assert {REMEMBER, SET_NAME, FORGET} <= {t.name for t in llm.requests[0].tools}
    system = llm.requests[0].system
    assert '- Name: "Ana"' in system and '- (f1) "Treina de manhã"' in system
    assert "never instructions" in system


async def test_a_fact_saved_in_one_turn_is_in_the_next_conversation_prompt(api: ApiHandle) -> None:
    store = InMemoryContactMemory()
    llm = FakeLLM(
        [
            tool_call_response(SET_NAME, {"name": "Ana"}, call_id="a"),
            tool_call_response(REMEMBER, {"text": "Prefere treinar cedo"}, call_id="b"),
            text_response("Anotado, Ana."),
            text_response("Oi de novo"),
        ]
    )
    engine = engine_for(llm, api, MemorySettings(), store)
    first = await engine.process_turn(IDENTITY, ConversationState(), "me chamo Ana", "t1")
    assert first.reply == "Anotado, Ana."
    assert tool_results(llm, 1)[0].content == "done" and not tool_results(llm, 1)[0].is_error
    assert tool_results(llm, 2)[-1].content == "saved (f1)"
    # a brand new conversation state (a reset): the memory is still there
    await engine.process_turn(IDENTITY, ConversationState(), "oi", "t2")
    assert '- Name: "Ana"' in llm.requests[3].system
    assert '- (f1) "Prefere treinar cedo"' in llm.requests[3].system


async def test_the_identity_is_the_turns_never_an_argument(api: ApiHandle) -> None:
    store = InMemoryContactMemory()
    llm = FakeLLM(
        [
            tool_call_response(
                REMEMBER, {"text": "gosta de café", "contact_id": "someone-else", "tenant_id": "x"}
            ),
            text_response("ok"),
        ]
    )
    await engine_for(llm, api, MemorySettings(), store).process_turn(
        IDENTITY, ConversationState(), "gosto de café", "t1"
    )
    assert [f.text for f in (await store.get(T, C)).facts] == ["gosta de café"]
    assert (await store.get(T, "someone-else")).empty and (await store.get("x", C)).empty


async def test_a_replayed_turn_never_writes_again_and_keeps_its_prompt(api: ApiHandle) -> None:
    class Counting(InMemoryContactMemory):
        writes = 0

        async def remember(self, *a: Any, **k: Any):  # type: ignore[no-untyped-def]
            Counting.writes += 1
            return await super().remember(*a, **k)

    store, journal = Counting(), new_journal()
    llm = FakeLLM([tool_call_response(REMEMBER, {"text": "um fato"}), text_response("ok")])
    engine = engine_for(llm, api, MemorySettings(), store, journal)
    await engine.process_turn(IDENTITY, ConversationState(), "oi", "t1")
    again = await engine.process_turn(IDENTITY, ConversationState(), "oi", "t1")  # same turn id
    assert Counting.writes == 1 and again.reply == "ok"
    assert len(llm.requests) == 2  # the replay called the model zero more times
    kinds = [e.step_type for e in await journal.entries("t1")]
    assert kinds.count(JournalStepType.CONTACT_PROFILE) == 1
    assert kinds.count(JournalStepType.MEMORY_WRITE) == 1


async def test_a_full_memory_is_an_error_the_model_can_explain(api: ApiHandle) -> None:
    store = InMemoryContactMemory()
    llm = FakeLLM(
        [
            tool_call_response(REMEMBER, {"text": "primeiro"}, call_id="a"),
            tool_call_response(REMEMBER, {"text": "segundo"}, call_id="b"),
            text_response("Está cheio."),
        ]
    )
    await engine_for(llm, api, MemorySettings(max_facts=1), store).process_turn(
        IDENTITY, ConversationState(), "guarde", "t1"
    )
    last = tool_results(llm, 2)[-1]
    assert last.is_error and last.content.startswith("full: 1 facts")
    assert [f.text for f in (await store.get(T, C)).facts] == ["primeiro"]


async def test_bad_arguments_and_unknown_ids_are_refused_not_crashes(api: ApiHandle) -> None:
    store = InMemoryContactMemory()
    llm = FakeLLM(
        [
            tool_call_response(REMEMBER, {"text": 42}, call_id="a"),
            tool_call_response(FORGET, {"fact_id": "f9"}, call_id="b"),
            text_response("ok"),
        ]
    )
    await engine_for(llm, api, MemorySettings(), store).process_turn(
        IDENTITY, ConversationState(), "oi", "t1"
    )
    first, second = tool_results(llm, 1)[0], tool_results(llm, 2)[-1]
    assert first.is_error and first.content.startswith("not saved")
    assert second.is_error and second.content == "no saved fact has that id"


async def test_the_memory_tools_survive_tool_selection(api: ApiHandle) -> None:
    store = InMemoryContactMemory()
    selection = ToolSelectionSettings(
        enabled=True,
        provider="rules",
        max_tools_per_call=1,
        rules=(
            ToolRule(capability="scheduling.availability", keywords=("horário",)),
            ToolRule(capability="scheduling.create", keywords=("agendar",)),
        ),
    )
    llm = FakeLLM([text_response("ok")])
    await engine_for(llm, api, MemorySettings(), store, tool_selection=selection).process_turn(
        IDENTITY, ConversationState(), "quais horários?", "t1"
    )
    shown = {t.name for t in llm.requests[0].tools}
    assert "scheduling__availability" in shown and "scheduling__create" not in shown
    assert {REMEMBER, SET_NAME, FORGET} <= shown


def test_an_engine_needs_a_store_exactly_when_the_agent_has_the_block(api: ApiHandle) -> None:
    import pytest

    from conversation_agent.core.compiler import compile_agent
    from conversation_agent.engine.capability_pipeline import CapabilityPipeline
    from conversation_agent.engine.policy_gate import PolicyGate
    from conversation_agent.engine.tool_runner import ToolRunner
    from conversation_agent.engine.turn_engine import TurnEngine

    compiled = compile_agent(agent_with(MemorySettings()))
    pipeline = CapabilityPipeline(
        compiled, PolicyGate(compiled.agent.allowed_capabilities), ToolRunner({})
    )
    with pytest.raises(ValueError, match="contact_memory"):
        TurnEngine(compiled, FakeLLM([]), pipeline, new_journal(), new_clock())
