"""Phase 14 (POC items 9, 10, 14): what a Choose can show and fill, and how dates read."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from conversation_agent.core.compiler import _flow_document
from conversation_agent.core.definitions.flow import (
    Choose,
    Collect,
    FlowDefinition,
    Invoke,
    Say,
    SlotDefinition,
)
from conversation_agent.core.definitions.flow import Slot as FlowSlot
from conversation_agent.core.errors import DefinitionError
from conversation_agent.core.models.flow import FlowInstance
from conversation_agent.core.models.tooling import CapabilityResult
from conversation_agent.engine.capability_pipeline import CapabilityOutcome
from conversation_agent.engine.flow_runner import FlowReply, FlowRunner, FlowTurn
from conversation_agent.tools.requests import build_capability_request
from engine.test_flow_hardening import rebuild
from vertical_slice.definitions import SCHEDULING_FLOW, build_agent

NOW = datetime(2026, 10, 5, 8, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))


def row(hour: int, court: str) -> dict[str, str]:
    return {"start_at": f"2026-10-06T{hour:02d}:00:00-03:00", "court": court}


ROWS = [row(9, "Quadra 1"), row(10, "Quadra 2"), row(19, "Quadra 1"), row(20, "Quadra 3")]


class IO:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows

    async def invoke(self, capability: str, args: dict[str, Any]) -> CapabilityOutcome:
        return CapabilityOutcome(
            result=CapabilityResult(status="success", data={"rows": self.rows})
        )

    async def extract(self, system: str, message: str, schema: Any) -> dict[str, Any]:
        raise AssertionError("the rules should have understood this")

    async def digress(self, note: str, text: str, capabilities: tuple[str, ...]) -> str:
        raise AssertionError("no digression expected")


def flow(**choose: Any) -> FlowDefinition:
    return FlowDefinition(
        name="booking",
        triggers=("reservar",),
        slots=(
            SlotDefinition(name="date", type="date", prompt="Para qual dia?"),
            SlotDefinition(
                name="period",
                type="enum",
                prompt="",
                required=False,
                choices={"morning": ("de manha", "manha"), "evening": ("a noite", "noite")},
                invalidates=("start_at", "court"),
            ),
            SlotDefinition(name="start_at", type="text", prompt="", required=False),
            SlotDefinition(name="court", type="text", prompt="", required=False),
        ),
        steps=(
            Collect(id="need", slots=("date",)),
            Invoke(
                id="search",
                capability="x.search",
                inputs={"day": FlowSlot(name="date")},
                default=Say(text="Não consegui consultar."),
            ),
            Choose(
                id="pick",
                source="search",
                list_field="rows",
                value_field="start_at",
                into="start_at",
                prompt="Opções:\n{options}\nQual?",
                empty=Say(text="Nada livre.", end=True),
                **choose,
            ),
        ),
        completed_reply="Feito.",
    )


class Talk:
    def __init__(self, definition: FlowDefinition, rows: list[dict[str, str]] = ROWS) -> None:
        agent = build_agent(flows=True).model_copy(update={"flows": (definition,)})
        self.runner = FlowRunner(agent)
        self.io = IO(rows)
        self.flows: tuple[FlowInstance, ...] = ()

    async def say(self, text: str) -> FlowReply:
        reply = await self.runner.handle(FlowTurn(text, self.flows, NOW, self.io))
        assert reply is not None
        self.flows = reply.flows
        return reply

    @property
    def slots(self) -> dict[str, Any]:
        return self.flows[-1].slots


# --- item 10: what an option says and what a pick fills ---


async def test_an_option_reads_as_its_label_not_as_a_raw_value() -> None:
    talk = Talk(flow(label="{court} - {start_at}", also={"court": "court"}))
    reply = await talk.say("quero reservar amanhã")
    assert "1) Quadra 1 - ter 06/10 às 09:00" in reply.reply  # not a raw ISO value
    reply = await talk.say("2")
    assert reply.reply == "Feito." and talk.flows == ()


async def test_a_pick_sets_the_primary_slot_and_the_extra_ones() -> None:
    from conversation_agent.engine.flow_runner import _picked

    step = flow(also={"court": "court"}).steps[-1]
    assert isinstance(step, Choose)
    option = {"value": "2026-10-06T09:00:00-03:00", "extras": {"court": "Quadra 1"}}
    assert _picked({"date": "2026-10-06"}, step, option) == {
        "date": "2026-10-06",
        "start_at": "2026-10-06T09:00:00-03:00",
        "court": "Quadra 1",
    }


async def test_with_one_option_left_it_is_taken_not_asked_when_the_flow_says_so() -> None:
    talk = Talk(flow(auto_select_single=True), [row(9, "Quadra 1")])
    reply = await talk.say("quero reservar amanhã")
    assert reply.reply == "Feito." and talk.flows == ()  # no question
    asking = Talk(flow(), [row(9, "Quadra 1")])  # default: ask even for one
    reply = await asking.say("quero reservar amanhã")
    assert "Opções:" in reply.reply and asking.flows[-1].awaiting_choice == "pick"


# --- item 9: a part of the day narrows what is shown ---


async def test_a_stated_part_of_the_day_shows_only_options_in_it() -> None:
    talk = Talk(flow(period_slot="period", no_match_text="Nada nesse período."))
    reply = await talk.say("quero reservar amanhã à noite")
    shown = reply.reply
    assert "19:00" in shown and "20:00" in shown and "09:00" not in shown
    assert "Nada nesse período." not in shown


async def test_a_part_of_the_day_with_no_option_says_so_and_shows_the_rest() -> None:
    talk = Talk(
        flow(period_slot="period", no_match_text="Nada nesse período."),
        [row(9, "Quadra 1"), row(10, "Quadra 2")],
    )
    reply = await talk.say("quero reservar amanhã à noite")
    assert reply.reply.startswith("Nada nesse período.") and "09:00" in reply.reply


async def test_changing_the_part_of_the_day_while_choosing_shows_the_new_list() -> None:
    talk = Talk(flow(period_slot="period"))
    reply = await talk.say("quero reservar amanhã")
    assert "09:00" in reply.reply  # no period yet: the first options
    reply = await talk.say("prefiro à noite")
    assert "19:00" in reply.reply and "09:00" not in reply.reply


async def test_a_period_can_be_redefined_by_the_flow() -> None:
    talk = Talk(flow(period_slot="period", period_hours={"evening": (20, 24)}))
    reply = await talk.say("quero reservar amanhã à noite")
    assert "20:00" in reply.reply and "19:00" not in reply.reply


# --- definitions ---


def test_the_new_fields_are_checked_and_stay_out_of_the_digest_when_unused() -> None:
    plain = _flow_document(flow())
    step = next(s for s in plain["steps"] if s["kind"] == "choose")
    assert not {"label", "also", "auto_select_single", "period_slot", "period_hours"} & set(step)
    used = _flow_document(flow(label="{court}", auto_select_single=True))
    step = next(s for s in used["steps"] if s["kind"] == "choose")
    assert step["label"] == "{court}" and step["auto_select_single"] is True

    with pytest.raises(DefinitionError, match="also"):
        flow(also={"nope": "court"})
    with pytest.raises(DefinitionError, match="0 <= from < to <= 24"):
        flow(period_slot="period", period_hours={"evening": (20, 30)})


def test_a_label_or_extra_field_the_items_do_not_have_does_not_compile() -> None:
    choose = next(s for s in SCHEDULING_FLOW.steps if isinstance(s, Choose))
    for bad in ({"label": "{nope}"}, {"also": {"start_at": "nope"}}):
        broken = SCHEDULING_FLOW.model_copy(
            update={
                "steps": tuple(
                    choose.model_copy(update=bad) if s is choose else s
                    for s in SCHEDULING_FLOW.steps
                )
            }
        )
        with pytest.raises(DefinitionError, match="no field 'nope'"):
            rebuild(build_agent(flows=True), flows=(broken,))


# --- item 14: dates in the confirmation question ---


def test_the_summary_of_a_proposal_reads_the_date_as_people_do() -> None:
    capability = next(c for c in build_agent().capabilities if c.name == "scheduling.create")
    request = build_capability_request(
        capability,
        {"service_id": "haircut", "start_at": "2026-10-06T10:00:00-03:00", "duration_minutes": 30},
    )
    assert request.summary == "Agendar haircut em ter 06/10 às 10:00 (30 min)"
