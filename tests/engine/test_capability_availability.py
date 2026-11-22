"""Capabilities per contact (ADR-033, INV-084/INV-085): a capability the port marks unavailable is not
shown to the model, its persona sections are left out, the PolicyGate refuses it if it is called
anyway, the answer is journaled, and without the port nothing changes."""

from __future__ import annotations

from typing import Any

import pytest

from conftest import ApiHandle
from conversation_agent.adapters.llm.fake import FakeLLM, text_response, tool_call_response
from conversation_agent.adapters.manifest.yaml_loader import load_manifest_file
from conversation_agent.core.compiler import compile_manifest
from conversation_agent.core.definitions.agent import AgentDefinition
from conversation_agent.core.definitions.persona_section import PersonaSection
from conversation_agent.core.errors import DefinitionError
from conversation_agent.core.models.conversation import ConversationState
from conversation_agent.core.models.journal import JournalStepType
from conversation_agent.core.models.llm import ToolResultPart
from support.builders import IDENTITY, new_clock, new_journal
from vertical_slice.definitions import build_agent
from vertical_slice.wiring import MANIFEST_PATH, build_engine

AVAILABILITY, CREATE = "scheduling.availability", "scheduling.create"
AVAILABILITY_TOOL, CREATE_TOOL = "scheduling__availability", "scheduling__create"
SECTION = PersonaSection(
    name="booking", requires_any=(CREATE,), text="SECTION-BOOKING: you can book."
)
OTHER = PersonaSection(
    name="slots", requires_any=(AVAILABILITY,), text="SECTION-SLOTS: you can list slots."
)


class Port:
    def __init__(self, *names: str, fail: bool = False) -> None:
        self.names, self.fail, self.calls = frozenset(names), fail, 0

    async def unavailable(self, tenant_id: str, contact_id: str) -> frozenset[str]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("the link service is down")
        return self.names


def agent_with(*sections: PersonaSection) -> AgentDefinition:
    agent = build_agent()
    fields = {n: getattr(agent, n) for n in AgentDefinition.model_fields}
    return AgentDefinition(**{**fields, "persona_sections": sections})


def engine_for(
    llm: FakeLLM, api: ApiHandle, port: Port | None, *sections: PersonaSection, journal=None
):  # type: ignore[no-untyped-def]
    engine, _, _ = build_engine(
        llm,
        api_base_url=api.base_url,
        journal=journal if journal is not None else new_journal(),
        clock=new_clock(),
        agent=agent_with(*sections),
        capability_availability=port,
    )
    return engine


def offered(llm: FakeLLM, call: int = 0) -> set[str]:
    return {t.name for t in llm.requests[call].tools}


async def test_without_the_port_everything_is_offered_and_nothing_is_journaled(
    api: ApiHandle,
) -> None:
    llm, journal = FakeLLM([text_response("oi")]), new_journal()
    await engine_for(llm, api, None, SECTION, OTHER, journal=journal).process_turn(
        IDENTITY, ConversationState(), "oi", "t1"
    )
    assert {AVAILABILITY_TOOL, CREATE_TOOL} <= offered(llm)
    assert "SECTION-BOOKING" in llm.requests[0].system and "SECTION-SLOTS" in llm.requests[0].system
    kinds = {e.step_type for e in await journal.entries("t1")}
    assert JournalStepType.CAPABILITY_AVAILABILITY not in kinds


async def test_an_unavailable_capability_is_hidden_with_its_prompt_section(api: ApiHandle) -> None:
    llm = FakeLLM([text_response("oi")])
    await engine_for(llm, api, Port(CREATE), SECTION, OTHER).process_turn(
        IDENTITY, ConversationState(), "oi", "t1"
    )
    assert AVAILABILITY_TOOL in offered(llm) and CREATE_TOOL not in offered(llm)
    system = llm.requests[0].system
    assert "SECTION-SLOTS" in system and "SECTION-BOOKING" not in system


async def test_a_section_stays_while_any_of_its_capabilities_is_offered(api: ApiHandle) -> None:
    both = PersonaSection(name="both", requires_any=(AVAILABILITY, CREATE), text="SECTION-BOTH")
    llm = FakeLLM([text_response("a"), text_response("b")])
    one = engine_for(llm, api, Port(CREATE), both)
    await one.process_turn(IDENTITY, ConversationState(), "oi", "t1")
    assert "SECTION-BOTH" in llm.requests[0].system
    none = engine_for(llm, api, Port(CREATE, AVAILABILITY), both)
    await none.process_turn(IDENTITY, ConversationState(), "oi", "t2")
    assert "SECTION-BOTH" not in llm.requests[1].system


async def test_a_hidden_capability_called_anyway_is_denied_by_the_gate(api: ApiHandle) -> None:
    llm = FakeLLM([tool_call_response(CREATE_TOOL, {}), text_response("não posso")])
    journal = new_journal()
    await engine_for(llm, api, Port(CREATE), journal=journal).process_turn(
        IDENTITY, ConversationState(), "agende", "t1"
    )
    result = [
        p for m in llm.requests[1].messages for p in m.parts if isinstance(p, ToolResultPart)
    ][-1]
    assert result.is_error  # nothing ran: the model got an error, not a booking
    decisions = [
        e.payload
        for e in await journal.entries("t1")
        if e.step_type == JournalStepType.POLICY_DECISION
    ]
    assert any("capability_unavailable" in str(d) for d in decisions)


async def test_the_answer_is_journaled_and_a_replay_does_not_ask_again(api: ApiHandle) -> None:
    port, journal = Port(CREATE), new_journal()
    llm = FakeLLM([text_response("ok")])
    engine = engine_for(llm, api, port, SECTION, journal=journal)
    await engine.process_turn(IDENTITY, ConversationState(), "oi", "t1")
    port.names = frozenset()  # the contact's situation changed in the meantime
    await engine.process_turn(IDENTITY, ConversationState(), "oi", "t1")  # same turn id: a replay
    assert port.calls == 1
    kinds = [e.step_type for e in await journal.entries("t1")]
    assert kinds.count(JournalStepType.CAPABILITY_AVAILABILITY) == 1


async def test_a_broken_port_restricts_nothing_and_does_not_stop_the_turn(api: ApiHandle) -> None:
    llm, journal = FakeLLM([text_response("oi")]), new_journal()
    outcome = await engine_for(llm, api, Port(fail=True), SECTION, journal=journal).process_turn(
        IDENTITY, ConversationState(), "oi", "t1"
    )
    assert outcome.reply == "oi"
    assert {AVAILABILITY_TOOL, CREATE_TOOL} <= offered(llm) and "SECTION-BOOKING" in llm.requests[
        0
    ].system
    entry = next(
        e
        for e in await journal.entries("t1")
        if e.step_type == JournalStepType.CAPABILITY_AVAILABILITY
    )
    assert entry.payload == {"unavailable": []}


async def test_names_the_agent_does_not_have_are_ignored(api: ApiHandle) -> None:
    llm = FakeLLM([text_response("oi")])
    await engine_for(llm, api, Port("ledger.balance")).process_turn(
        IDENTITY, ConversationState(), "oi", "t1"
    )
    assert {AVAILABILITY_TOOL, CREATE_TOOL} <= offered(llm)


def test_a_section_must_require_allowed_capabilities() -> None:
    bad = PersonaSection(name="x", requires_any=("ledger.balance",), text="t")
    with pytest.raises(DefinitionError, match="not an allowed capability"):
        agent_with(bad)
    twice = (SECTION, SECTION)
    with pytest.raises(DefinitionError, match="duplicate persona section"):
        agent_with(*twice)


def test_the_digest_only_changes_for_agents_that_use_sections() -> None:
    manifest: dict[str, Any] = load_manifest_file(MANIFEST_PATH)
    plain = compile_manifest(manifest)
    assert compile_manifest(load_manifest_file(MANIFEST_PATH)).digest == plain.digest
    with_sections = compile_manifest(
        {**manifest, "persona_sections": [{"name": "b", "requires_any": [CREATE], "text": "t"}]}
    )
    assert with_sections.digest != plain.digest
    assert with_sections.agent.persona_sections[0].text == "t"
