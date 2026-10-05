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
            SlotDefinition(
                name="preferred_time",
                type="time",
                prompt="",
                required=False,
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


# --- POC items 7, 8 and 6 ---


def starts(definition: FlowDefinition, text: str) -> bool:
    agent = build_agent(flows=True).model_copy(update={"flows": (definition,)})
    return FlowRunner(agent)._trigger(text, None) is not None


def test_a_trigger_word_in_a_question_starts_the_flow_unless_the_flow_opts_out() -> None:
    assert starts(flow(), "quanto custa reservar uma quadra?")  # the old behaviour, unchanged
    careful = flow().model_copy(update={"start_on_questions": False})
    assert not starts(careful, "quanto custa reservar uma quadra?")  # the agent answers it
    assert starts(careful, "quero reservar uma quadra")  # a request still starts it


def test_two_flows_with_the_same_trigger_at_the_same_priority_do_not_compile() -> None:
    from conversation_agent.core.compiler import check_agent

    agent = build_agent(flows=True)
    twin = SCHEDULING_FLOW.model_copy(update={"name": "twin"})
    broken = agent.model_copy(update={"flows": (SCHEDULING_FLOW, twin)})
    codes = [d.code for d in check_agent(broken)]
    assert "FLOW_TRIGGER_TIE" in codes
    fixed = broken.model_copy(
        update={"flows": (SCHEDULING_FLOW, twin.model_copy(update={"priority": 1}))}
    )
    assert "FLOW_TRIGGER_TIE" not in [d.code for d in check_agent(fixed)]


def test_a_tie_between_different_phrases_is_logged_not_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    a = flow().model_copy(update={"name": "a", "triggers": ("cancelar",)})
    b = flow().model_copy(update={"name": "b", "triggers": ("reserva",)})
    agent = build_agent(flows=True).model_copy(update={"flows": (a, b)})
    assert FlowRunner(agent)._trigger("cancelar minha reserva", None) is None
    assert "flow trigger tie: a, b" in caplog.text


def test_replies_split_into_paragraphs_only_up_to_a_limit() -> None:
    from conversation_agent.engine.turn_coordinator import MAX_REPLY_PARTS, reply_parts

    assert reply_parts("só uma linha\ncom quebra simples") == ["só uma linha\ncom quebra simples"]
    assert reply_parts("resposta\n\nQual horário?") == ["resposta", "Qual horário?"]
    many = "\n\n".join(f"p{i}" for i in range(7))
    parts = reply_parts(many)
    assert len(parts) == MAX_REPLY_PARTS and parts[-1] == "p3\n\np4\n\np5\n\np6"
    assert reply_parts("   ") == ["   "]


# --- several rows share the stated time (one per court) ---

BUSY = [
    row(8, "Quadra 1"),
    row(8, "Quadra 2"),
    row(9, "Quadra 1"),
    row(18, "Quadra 1"),
    row(18, "Quadra 2"),
]
PER_COURT = {
    "label": "{court} - {start_at}",
    "also": {"court": "court"},
    "prefer_time_slot": "preferred_time",
    "no_match_text": "Não tenho exatamente esse horário.",
}


async def test_a_stated_time_that_fits_several_rows_shows_exactly_those() -> None:
    talk = Talk(flow(**PER_COURT), BUSY)
    reply = await talk.say("quero reservar amanhã às 18h")
    assert "Quadra 1 - ter 06/10 às 18:00" in reply.reply
    assert "Quadra 2 - ter 06/10 às 18:00" in reply.reply
    assert "08:00" not in reply.reply and "09:00" not in reply.reply
    assert "Não tenho exatamente" not in reply.reply  # it HAS that time: twice
    assert len(talk.flows[-1].options["pick"]) == 2  # a number picks among these two


async def test_the_court_can_then_be_named_in_words() -> None:
    talk = Talk(flow(**PER_COURT), BUSY)
    await talk.say("quero reservar amanhã às 18h")
    reply = await talk.say("quadra 2")
    assert reply.reply == "Feito." and talk.flows == ()


async def test_saying_the_time_while_the_full_list_is_shown_narrows_it() -> None:
    talk = Talk(flow(**PER_COURT), BUSY)
    first = await talk.say("quero reservar amanhã")
    assert "08:00" in first.reply  # no time yet: the first rows
    reply = await talk.say("18h")  # used to repeat the same list
    assert "18:00" in reply.reply and "08:00" not in reply.reply
    assert "Não tenho exatamente" not in reply.reply


async def test_a_time_nobody_has_still_says_so_and_shows_everything() -> None:
    talk = Talk(flow(**PER_COURT), BUSY)
    reply = await talk.say("quero reservar amanhã às 17h")
    assert reply.reply.startswith("Não tenho exatamente esse horário.") and "08:00" in reply.reply


async def test_naming_the_court_and_the_time_together_picks_the_row() -> None:
    talk = Talk(flow(**PER_COURT), BUSY)
    await talk.say("quero reservar amanhã")
    reply = await talk.say("quadra 2 às 18h")
    assert reply.reply == "Feito." and talk.flows == ()


def test_words_that_do_not_tell_the_rows_apart_never_pick_one() -> None:
    from conversation_agent.engine.flow_understanding import _by_label

    rows = [{"label": "Quadra 1 - 18:00"}, {"label": "Quadra 2 - 18:00"}]
    assert _by_label("quadra", rows) is None  # in every row: says nothing
    assert _by_label("quero uma quadra boa", rows) is None
    assert _by_label("a quadra 2", rows) == rows[1]
    assert _by_label("1", rows) == rows[0]
