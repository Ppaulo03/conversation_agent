"""Phase 4.1: effective risk as the one authority, Propose semantics, routing, stack bounds."""

from __future__ import annotations

from typing import Any

import pytest

from conftest import ApiHandle
from conversation_agent.core.definitions.agent import AgentDefinition
from conversation_agent.core.definitions.flow import (
    AddDays,
    Ask,
    Choose,
    Collect,
    FlowDefinition,
    Invoke,
    Propose,
    Say,
    Table,
)
from conversation_agent.core.definitions.flow import Slot as FlowSlot
from conversation_agent.core.definitions.tool import RecoverySpec
from conversation_agent.core.errors import DefinitionError
from conversation_agent.engine.flow_runner import FlowRunner
from engine.test_flows import PRICES, Chat, understood
from vertical_slice.definitions import (
    AVAILABILITY_BINDING,
    ERP_GET_AVAILABLE_SLOTS,
    LOOKUP_BINDING,
    SCHEDULING_FLOW,
    build_agent,
)


def rebuild(agent: AgentDefinition, **over: Any) -> AgentDefinition:
    """Re-run full validation (model_copy would skip it)."""
    fields = {name: getattr(agent, name) for name in AgentDefinition.model_fields}
    return AgentDefinition(**{**fields, **over})


def swap(items: tuple[Any, ...], old: Any, new: Any) -> tuple[Any, ...]:
    return tuple(new if i is old else i for i in items)


def agent_with_protected_availability(**over: Any) -> AgentDefinition:
    """A capability LABELLED read whose binding is irreversible: effective risk decides."""
    agent = build_agent(flows=True)
    protected = AVAILABILITY_BINDING.model_copy(update={"risk": "irreversible"})
    bindings = tuple(
        protected if b.capability == "scheduling.availability" else b for b in agent.bindings
    )
    return rebuild(agent, bindings=bindings, **over)


# --- effective risk is the authority ---


def test_invoke_rejects_a_capability_that_is_only_a_read_by_label() -> None:
    with pytest.raises(DefinitionError, match="invoke is for reads"):
        agent_with_protected_availability()


def test_digression_rejects_a_capability_that_is_only_a_read_by_label() -> None:
    flow = SCHEDULING_FLOW.model_copy(update={"steps": SCHEDULING_FLOW.steps[:1]})
    with pytest.raises(DefinitionError, match="digression may only use effective read"):
        agent_with_protected_availability(flows=(flow,))


def test_safe_retry_is_rejected_for_an_effective_write() -> None:
    agent = build_agent()
    unsafe_tool = ERP_GET_AVAILABLE_SLOTS.model_copy(
        update={"recovery": RecoverySpec(strategy="safe_retry")}
    )
    protected = AVAILABILITY_BINDING.model_copy(update={"risk": "irreversible"})
    with pytest.raises(DefinitionError, match="safe_retry"):
        rebuild(
            agent,
            tools=swap(agent.tools, ERP_GET_AVAILABLE_SLOTS, unsafe_tool),
            bindings=swap(agent.bindings, AVAILABILITY_BINDING, protected),
        )


def test_a_status_lookup_must_be_an_effective_read_not_just_labelled_one() -> None:
    agent = build_agent()
    protected = LOOKUP_BINDING.model_copy(update={"risk": "irreversible"})
    with pytest.raises(DefinitionError, match="effective read"):
        rebuild(agent, bindings=swap(agent.bindings, LOOKUP_BINDING, protected))


# --- Propose semantics ---


def flow_with(steps: tuple[Any, ...], **kw: Any) -> FlowDefinition:
    return FlowDefinition(name="f", slots=PRICES.slots, steps=steps, **kw)


def propose(step_id: str = "p") -> Propose:
    return Propose(
        id=step_id,
        capability="scheduling.create",
        inputs={"service_id": FlowSlot(name="service")},
        default=Say(text="x", end=True),
    )


def test_propose_must_be_the_single_last_step() -> None:
    with pytest.raises(DefinitionError, match="single, last"):
        flow_with((propose(), Collect(id="again", slots=("service",))))
    with pytest.raises(DefinitionError, match="single, last"):
        flow_with((propose("a"), propose("b")))
    flow_with((Collect(id="c", slots=("service",)), propose()))  # the supported shape


# --- semantic checks that would otherwise only fail in production ---


def test_a_table_must_cover_every_choice_of_its_enum_slot() -> None:
    inputs = {"duration_minutes": Table(key=FlowSlot(name="service"), mapping={"haircut": 30})}
    step = Invoke(id="s", capability="c", inputs=inputs, default=Say(text="x"))
    with pytest.raises(DefinitionError, match="map every choice"):
        flow_with((step,))


def test_add_days_needs_a_date_slot_and_choose_needs_options() -> None:
    step = Invoke(
        id="s",
        capability="c",
        inputs={"to": AddDays(base=FlowSlot(name="service"), days=1)},
        default=Say(text="x"),
    )
    with pytest.raises(DefinitionError, match="needs a date slot"):
        flow_with((step,))
    with pytest.raises(DefinitionError, match="max_options"):
        flow_with(
            (
                Invoke(id="s", capability="c", inputs={}, default=Say(text="x")),
                Choose(
                    id="c",
                    source="s",
                    list_field="l",
                    value_field="v",
                    into="service",
                    prompt="{options}",
                    empty=Ask(slot="service"),
                    max_options=0,
                ),
            )
        )


def test_choose_fields_must_exist_in_the_source_capability_output() -> None:
    agent = build_agent(flows=True)
    steps = tuple(
        s.model_copy(update={"list_field": "slotz"}) if isinstance(s, Choose) else s
        for s in SCHEDULING_FLOW.steps
    )
    with pytest.raises(DefinitionError, match="no output field 'slotz'"):
        rebuild(agent, flows=(SCHEDULING_FLOW.model_copy(update={"steps": steps}),))
    steps = tuple(
        s.model_copy(update={"value_field": "starts"}) if isinstance(s, Choose) else s
        for s in SCHEDULING_FLOW.steps
    )
    with pytest.raises(DefinitionError, match="no field 'starts'"):
        rebuild(agent, flows=(SCHEDULING_FLOW.model_copy(update={"steps": steps}),))


def test_an_unknown_timezone_is_rejected_when_flows_depend_on_it() -> None:
    with pytest.raises(DefinitionError, match="timezone"):
        rebuild(build_agent(flows=True), timezone="Mars/Olympus")


# --- trigger routing is not order-dependent ---


def two_flow_agent(**flow_overrides: Any) -> AgentDefinition:
    return rebuild(
        build_agent(),
        flows=(
            SCHEDULING_FLOW,
            PRICES.model_copy(update=flow_overrides),
        ),
    )


def pick(agent: AgentDefinition, text: str) -> str | None:
    found = FlowRunner(agent)._trigger(text, None)
    return found.name if found else None


def test_the_most_specific_trigger_wins_regardless_of_declaration_order() -> None:
    agent = two_flow_agent()
    assert pick(agent, "quanto custa a consulta?") == "prices"  # 2-word phrase beats "consulta"
    assert pick(agent, "qual o preco da consulta?") is None  # equally specific: no guessing
    reversed_agent = rebuild(agent, flows=tuple(reversed(agent.flows)))
    assert pick(reversed_agent, "quanto custa a consulta?") == "prices"
    assert pick(agent, "quero agendar") == "scheduling"


def test_priority_breaks_ties_and_a_true_tie_starts_nothing() -> None:
    agent = two_flow_agent(priority=1)
    assert pick(agent, "o preco da consulta") == "prices"  # explicit priority wins
    a = SCHEDULING_FLOW.model_copy(update={"triggers": ("alfa",)})
    b = PRICES.model_copy(update={"triggers": ("beta",)})
    assert pick(rebuild(build_agent(), flows=(a, b)), "alfa beta") is None  # no guessing


def test_the_same_trigger_in_two_flows_is_a_definition_error() -> None:
    clash = PRICES.model_copy(update={"triggers": ("Agendar",)})
    with pytest.raises(DefinitionError, match="declared by both"):
        rebuild(build_agent(), flows=(SCHEDULING_FLOW, clash))


# --- the flow stack is bounded ---


async def test_alternating_between_two_flows_never_duplicates_instances(api: ApiHandle) -> None:
    chat = Chat(api, extra_flows=True)
    await chat.say("Quero agendar")
    await chat.say("quanto custa?")
    for _ in range(4):
        out = await chat.say("agendar")
        assert "Qual serviço você quer" in out.reply  # the parked scheduling flow, resumed
        await chat.say("quanto custa?")
    assert [f.flow_name for f in chat.state.flows] == ["scheduling", "prices"]


async def test_depth_limit_ignores_a_new_flow_instead_of_growing_state(api: ApiHandle) -> None:
    chat = Chat(api, extra_flows=True, max_depth=1, script=[understood("other")])
    await chat.say("Quero agendar")
    out = await chat.say("quanto custa?")  # would need depth 2
    assert [f.flow_name for f in chat.state.flows] == ["scheduling"]
    assert out.reply.startswith("Não entendi")


# --- INV-030: one compiled root for the whole runtime graph ---


def test_the_engine_refuses_a_pipeline_built_from_another_compiled_agent() -> None:
    from types import SimpleNamespace

    from conversation_agent.adapters.llm.fake import FakeLLM
    from conversation_agent.core.compiler import compile_agent
    from conversation_agent.engine.turn_engine import TurnEngine
    from support.builders import new_clock, new_journal
    from vertical_slice.wiring import build_pipeline

    pipeline, _, _ = build_pipeline(api_base_url="x", providers={})  # compiled WITHOUT flows
    with pytest.raises(ValueError, match="different compiled agent"):
        TurnEngine(
            compile_agent(build_agent(flows=True)),
            FakeLLM([]),
            pipeline,
            new_journal(),
            new_clock(),
        )
    engine = TurnEngine(pipeline.compiled, FakeLLM([]), pipeline, new_journal(), new_clock())
    other = SimpleNamespace(compiled=compile_agent(build_agent(flows=True)))
    with pytest.raises(ValueError, match="confirmation stage"):
        engine.attach_confirmation(other)  # type: ignore[arg-type]
