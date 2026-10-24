"""Tool selection: the model sees a subset of the allowed tools, never more (ADR tool-selection)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from conftest import ApiHandle
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.tool_selection.factory import tool_selector_for
from conversation_agent.adapters.tool_selection.rules import RulesToolSelector
from conversation_agent.core.definitions.agent import AgentDefinition
from conversation_agent.core.definitions.tool_selection import ToolRule, ToolSelectionSettings
from conversation_agent.core.errors import DefinitionError
from conversation_agent.core.models.conversation import ConversationState
from conversation_agent.core.models.journal import JournalStepType
from conversation_agent.core.models.llm import LLMToolDefinition
from conversation_agent.ports.tool_selector import ToolScore
from support.builders import IDENTITY, new_clock, new_journal
from vertical_slice.definitions import build_agent
from vertical_slice.wiring import build_engine

AVAIL = "scheduling__availability"
CREATE = "scheduling__create"


def settings(**over: Any) -> ToolSelectionSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "provider": "rules",
        "max_tools_per_call": 1,
        "rules": (
            ToolRule(capability="scheduling.availability", keywords=("horário", "disponível")),
            ToolRule(capability="scheduling.create", keywords=("agendar",)),
        ),
    }
    return ToolSelectionSettings(**{**base, **over})


def agent_with(sel: ToolSelectionSettings | None) -> AgentDefinition:
    agent = build_agent()
    fields = {n: getattr(agent, n) for n in AgentDefinition.model_fields}
    return AgentDefinition(**{**fields, "tool_selection": sel})


def engine_for(llm: FakeLLM, api: ApiHandle, sel: ToolSelectionSettings | None, journal=None, **kw):  # type: ignore[no-untyped-def]
    engine, _, _ = build_engine(
        llm,
        api_base_url=api.base_url,
        journal=journal if journal is not None else new_journal(),
        clock=new_clock(),
        agent=agent_with(sel),
        **kw,
    )
    return engine


def shown(llm: FakeLLM, call: int = 0) -> set[str]:
    return {t.name for t in llm.requests[call].tools}


async def test_off_by_default_the_model_sees_every_allowed_tool(api: ApiHandle) -> None:
    llm = FakeLLM([text_response("ok")])
    await engine_for(llm, api, None).process_turn(IDENTITY, ConversationState(), "oi", "t1")
    assert shown(llm) == {AVAIL, CREATE}


async def test_over_the_limit_only_relevant_tools_are_shown_and_it_is_journaled(
    api: ApiHandle,
) -> None:
    llm, journal = FakeLLM([text_response("ok")]), new_journal()
    await engine_for(llm, api, settings(), journal).process_turn(
        IDENTITY, ConversationState(), "quais horários disponíveis?", "t1"
    )
    assert shown(llm) == {AVAIL}  # subset of allowed, create was never added or kept
    entry = next(
        e for e in await journal.entries("t1") if e.step_type is JournalStepType.TOOL_SELECTION
    )
    assert entry.payload["mode"] == "selected" and entry.payload["kept"] == [AVAIL]


async def test_below_every_limit_nothing_is_filtered(api: ApiHandle) -> None:
    llm = FakeLLM([text_response("ok")])
    await engine_for(llm, api, settings(max_tools_per_call=2)).process_turn(
        IDENTITY, ConversationState(), "quais horários?", "t1"
    )
    assert shown(llm) == {AVAIL, CREATE}


async def test_the_token_limit_triggers_on_its_own(api: ApiHandle) -> None:
    llm = FakeLLM([text_response("ok")])
    sel = settings(max_tools_per_call=None, max_tool_tokens=1)
    await engine_for(llm, api, sel).process_turn(IDENTITY, ConversationState(), "horário", "t1")
    assert shown(llm) == {AVAIL}


async def test_always_include_is_kept(api: ApiHandle) -> None:
    llm = FakeLLM([text_response("ok")])
    sel = settings(always_include=("scheduling.create",))
    await engine_for(llm, api, sel).process_turn(IDENTITY, ConversationState(), "horário", "t1")
    assert shown(llm) == {AVAIL, CREATE}


async def test_no_match_falls_back_to_the_full_list(api: ApiHandle) -> None:
    llm = FakeLLM([text_response("ok")])
    await engine_for(llm, api, settings()).process_turn(IDENTITY, ConversationState(), "oi", "t1")
    assert shown(llm) == {AVAIL, CREATE}


async def test_the_model_asking_for_a_removed_tool_repeats_once_with_the_full_list(
    api: ApiHandle,
) -> None:
    llm = FakeLLM([_call(CREATE), text_response("pronto")])
    out = await engine_for(llm, api, settings()).process_turn(
        IDENTITY, ConversationState(), "quais horários?", "t1"
    )
    assert shown(llm, 0) == {AVAIL} and shown(llm, 1) == {AVAIL, CREATE}
    assert out.reply is not None


def _call(name: str):  # type: ignore[no-untyped-def]
    from conversation_agent.adapters.llm.fake import tool_call_response

    return tool_call_response(name, {"booking_id": "x"})


class _Boom:
    async def rank(
        self, message: str, candidates: tuple[LLMToolDefinition, ...], *, tenant_id: str
    ):  # type: ignore[no-untyped-def]
        raise RuntimeError("down")


class _Inventor:
    async def rank(
        self, message: str, candidates: tuple[LLMToolDefinition, ...], *, tenant_id: str
    ):  # type: ignore[no-untyped-def]
        return [ToolScore("secret__tool", 1.0), ToolScore(AVAIL, 1.0)]


async def test_a_failing_selector_means_the_full_list(api: ApiHandle) -> None:
    llm = FakeLLM([text_response("ok")])
    await engine_for(llm, api, settings(), tool_selector=_Boom()).process_turn(
        IDENTITY, ConversationState(), "horário", "t1"
    )
    assert shown(llm) == {AVAIL, CREATE}


async def test_a_selector_can_never_add_a_tool(api: ApiHandle) -> None:
    llm = FakeLLM([text_response("ok")])
    await engine_for(llm, api, settings(), tool_selector=_Inventor()).process_turn(
        IDENTITY, ConversationState(), "horário", "t1"
    )
    assert shown(llm) == {AVAIL}


async def test_replay_reuses_the_journaled_decision(api: ApiHandle) -> None:
    journal = new_journal()
    first = FakeLLM([text_response("ok")])
    await engine_for(first, api, settings(), journal).process_turn(
        IDENTITY, ConversationState(), "horário", "t1"
    )
    second = FakeLLM([])
    await engine_for(second, api, settings(), journal, tool_selector=_Boom()).process_turn(
        IDENTITY, ConversationState(), "horário", "t1"
    )
    assert second.calls == 0  # nothing re-run: the stored decision (and responses) were reused


async def test_the_fallback_rate_switches_the_filter_off(api: ApiHandle) -> None:
    from conversation_agent.engine.tool_selection import MIN_SAMPLES, SelectionMonitor

    monitor = SelectionMonitor("a", "1.0.0")
    for _ in range(MIN_SAMPLES):
        monitor.note(fell_back=True, max_rate=0.05)
    assert monitor.disabled
    llm = FakeLLM([text_response("ok")])
    await engine_for(llm, api, settings(), selection_monitor=monitor).process_turn(
        IDENTITY, ConversationState(), "horário", "t1"
    )
    assert shown(llm) == {AVAIL, CREATE}


async def test_rules_match_without_accents_or_case() -> None:
    tools = tuple(LLMToolDefinition(name=n, description="", input_schema={}) for n in (AVAIL,))
    sel = RulesToolSelector(settings().rules)
    scores = await sel.rank("HORARIO por favor", tools, tenant_id="t")
    assert scores == [ToolScore(AVAIL, 0.5)]


def test_at_least_one_limit_is_required_and_either_may_be_off() -> None:
    with pytest.raises(ValidationError, match="max_tools_per_call and/or max_tool_tokens"):
        settings(max_tools_per_call=None)
    assert settings(max_tools_per_call=None, max_tool_tokens=100).max_tool_tokens == 100
    assert settings(max_tool_tokens=100).max_tools_per_call == 1  # both set: first exceeded wins


def test_rules_must_name_allowed_capabilities() -> None:
    bad = settings(rules=(ToolRule(capability="scheduling.nope", keywords=("x",)),))
    with pytest.raises(DefinitionError, match="unknown capability"):
        agent_with(bad)


def test_a_disabled_or_absent_selection_has_no_selector() -> None:
    assert tool_selector_for(agent_with(None)) is None
    assert tool_selector_for(agent_with(ToolSelectionSettings())) is None
