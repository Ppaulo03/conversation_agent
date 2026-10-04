"""Phase 4: Flows. Deterministic conversation on top of the real capability path.

No LLM is scripted anywhere in this file: every turn is answered by the Flow, and each test
asserts the model was never called. The scheduling API is the real reference service.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from conftest import ApiHandle
from conversation_agent.adapters.llm.fake import (
    FakeLLM,
    Script,
    text_response,
    tool_call_response,
)
from conversation_agent.adapters.tools.fake import FakeToolProvider
from conversation_agent.core.definitions.flow import Collect, FlowDefinition, SlotDefinition
from conversation_agent.core.definitions.flow import Slot as FlowSlot
from conversation_agent.core.errors import DefinitionError
from conversation_agent.core.models.conversation import ConversationState, TurnOutcome
from conversation_agent.core.models.llm import LLMResponse, LLMStopReason, LLMUsage
from conversation_agent.core.models.tooling import ToolError, ToolResult
from conversation_agent.engine.policy_gate import PolicyGate
from conversation_agent.engine.turn_engine import TurnEngine
from support.builders import IDENTITY, new_clock, new_journal
from vertical_slice.definitions import SCHEDULING_FLOW, build_agent
from vertical_slice.wiring import build_engine, load_compiled_agent

PRICES = FlowDefinition(
    name="prices",
    triggers=("quanto custa", "preco", "valor"),
    slots=(
        SlotDefinition(
            name="service",
            type="enum",
            prompt="De qual serviço: corte ou consulta?",
            choices={"haircut": ("corte",), "consultation": ("consulta",)},
        ),
    ),
    steps=(Collect(id="need", slots=("service",)),),
    completed_reply="Os valores são informados na recepção.",
)


class Chat:
    """One conversation: carries state across turns like the coordinator would."""

    def __init__(
        self,
        api: ApiHandle,
        *,
        extra_flows: bool = False,
        script: list[Script] | None = None,
        max_depth: int = 3,
        from_manifest: bool = False,
    ) -> None:
        self.strict = script is None
        self.llm = FakeLLM(script or [])  # strict chats fail on ANY model call
        self.state = ConversationState()
        self.journal = new_journal()
        self.n = 0
        override = None
        if extra_flows:
            override = build_agent(flows=True).model_copy(
                update={"flows": (SCHEDULING_FLOW, PRICES), "max_flow_depth": max_depth}
            )
        elif from_manifest:
            override = load_compiled_agent().agent
        engine, _, _ = build_engine(
            self.llm,
            api_base_url=api.base_url,
            journal=self.journal,
            clock=new_clock(),
            flows=True,
            agent=override,
        )
        self.engine = engine

    async def say(self, text: str, turn_id: str | None = None) -> TurnOutcome:
        self.n += 1
        outcome = await self.engine.process_turn(
            IDENTITY, self.state, text, turn_id or f"turn-{self.n}"
        )
        self.state = outcome.state
        if self.strict:
            assert self.llm.requests == []  # the Flow answered; the model was never involved
        return outcome

    @property
    def flow(self) -> Any:
        return self.state.active_flow


def proposed_start(outcome: TurnOutcome) -> datetime:
    assert len(outcome.proposed) == 1
    return datetime.fromisoformat(str(outcome.proposed[0].request.args["start_at"])).astimezone(UTC)


async def test_everything_in_one_message_reaches_a_proposal(api: ApiHandle) -> None:
    chat = Chat(api)
    out = await chat.say("Quero marcar um corte amanhã às 10h")
    assert proposed_start(out) == datetime(2026, 10, 6, 13, 0, tzinfo=UTC)  # 10:00 São Paulo
    assert "Posso confirmar" in out.reply
    assert len(api.availability_requests()) == 1  # one real search, nothing invented


async def test_missing_slots_are_asked_one_at_a_time(api: ApiHandle) -> None:
    chat = Chat(api)
    out = await chat.say("Quero agendar")
    assert "Qual serviço" in out.reply
    out = await chat.say("uma consulta")
    assert "Para qual dia" in out.reply
    out = await chat.say("amanhã")
    assert "Tenho estes horários para ter 06/10" in out.reply
    assert "1) ter 06/10 às 09:00" in out.reply


async def test_user_picks_one_of_the_real_options_by_number(api: ApiHandle) -> None:
    chat = Chat(api)
    await chat.say("Quero agendar uma consulta amanhã")
    out = await chat.say("2")
    second = chat.flow.slots["start_at"]
    assert proposed_start(out) == datetime.fromisoformat(second).astimezone(UTC)
    assert chat.flow.options["pick"][1]["value"] == second  # it was one the tool returned


async def test_correcting_the_time_keeps_context_and_does_not_search_again(
    api: ApiHandle,
) -> None:
    chat = Chat(api)
    await chat.say("Quero marcar um corte amanhã às 10h")
    out = await chat.say("na verdade às 11h")
    assert proposed_start(out) == datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
    assert chat.flow.slots["service"] == "haircut" and chat.flow.slots["date"] == "2026-10-06"
    assert len(api.availability_requests()) == 1  # same search inputs: result still valid


async def test_correcting_the_day_searches_again_and_keeps_the_rest(api: ApiHandle) -> None:
    chat = Chat(api)
    await chat.say("Quero marcar um corte amanhã às 10h")
    out = await chat.say("pode ser quarta?")
    assert proposed_start(out) == datetime(2026, 10, 7, 13, 0, tzinfo=UTC)  # still 10:00
    assert chat.flow.slots["service"] == "haircut"
    assert len(api.availability_requests()) == 2  # the new day needed a new search


async def test_correcting_the_service_discards_the_old_choice(api: ApiHandle) -> None:
    chat = Chat(api)
    await chat.say("Quero agendar um corte amanhã")
    await chat.say("1")
    out = await chat.say("melhor uma consulta")
    assert chat.flow.slots["service"] == "consultation"
    assert "start_at" not in chat.flow.slots  # the slot picked for the haircut is gone
    assert "Tenho estes horários" in out.reply  # offered again from a fresh search


async def test_a_time_that_does_not_exist_shows_the_real_options(api: ApiHandle) -> None:
    chat = Chat(api)
    out = await chat.say("Quero marcar um corte amanhã às 23h")
    assert "Não tenho exatamente esse horário" in out.reply
    assert "1) ter 06/10 às 09:00" in out.reply
    assert not out.proposed


async def test_no_availability_asks_for_another_day(api: ApiHandle) -> None:
    api.state.fully_booked_dates = {datetime(2026, 10, 6).date()}
    chat = Chat(api)
    out = await chat.say("Quero marcar um corte amanhã")
    assert "Não tenho horários livres em ter 06/10" in out.reply
    assert "Para qual dia" in out.reply
    out = await chat.say("quarta")
    assert "Tenho estes horários para qua 07/10" in out.reply


async def test_http_failure_stays_in_the_flow_and_retries_on_next_message(
    api: ApiHandle,
) -> None:
    api.state.fault = {"status": 503}
    chat = Chat(api)
    out = await chat.say("Quero marcar um corte amanhã às 10h")
    assert "Não consegui consultar a agenda" in out.reply
    assert chat.flow is not None and not out.proposed
    api.state.fault = None
    out = await chat.say("tente de novo")
    assert proposed_start(out) == datetime(2026, 10, 6, 13, 0, tzinfo=UTC)


async def test_cancel_closes_the_flow_without_touching_anything(api: ApiHandle) -> None:
    chat = Chat(api)
    await chat.say("Quero agendar um corte")
    out = await chat.say("deixa pra lá")
    assert out.reply == "Tudo bem, não vou agendar nada."
    assert chat.state.flows == () and api.requests == []


async def test_a_different_request_suspends_the_flow_and_resumes_it(api: ApiHandle) -> None:
    chat = Chat(api, extra_flows=True)
    await chat.say("Quero agendar um corte")
    out = await chat.say("quanto custa?")
    assert [f.status for f in chat.state.flows] == ["suspended", "active"]
    assert "De qual serviço" in out.reply
    out = await chat.say("a consulta")
    assert "Os valores são informados na recepção." in out.reply
    assert "Voltando ao que estávamos fazendo" in out.reply
    assert "Para qual dia" in out.reply
    assert [(f.flow_name, f.status) for f in chat.state.flows] == [("scheduling", "active")]
    assert chat.flow.slots == {"service": "haircut"}  # nothing was lost


async def test_unintelligible_answers_are_bounded(api: ApiHandle) -> None:
    chat = Chat(api, script=[understood("other")] * 4)
    await chat.say("Quero agendar um corte")
    for _ in range(3):
        out = await chat.say("hmmm blá")
        assert out.reply.startswith("Não entendi.") and "Para qual dia" in out.reply
    out = await chat.say("hmmm blá")
    assert "vou encerrar" in out.reply and chat.state.flows == ()
    assert chat.llm.calls == 4  # one bounded model call per unintelligible message


async def test_flow_turns_replay_from_the_journal_without_repeating_effects(
    api: ApiHandle,
) -> None:  # INV-014
    chat = Chat(api)
    first = await chat.say("Quero marcar um corte amanhã às 10h", turn_id="t-replay")
    searches = len(api.availability_requests())
    again = await chat.engine.process_turn(
        IDENTITY, ConversationState(), "Quero marcar um corte amanhã às 10h", "t-replay"
    )
    assert again.reply == first.reply and again.state.flows == first.state.flows
    assert len(api.availability_requests()) == searches  # nothing was executed twice


async def test_agents_without_flows_keep_the_agent_loop() -> None:
    assert build_agent().flows == () and len(build_agent(flows=True).flows) == 1


def understood(kind: str, **extra: Any) -> LLMResponse:
    return LLMResponse(
        parts=(),
        stop_reason=LLMStopReason.END_TURN,
        usage=LLMUsage(input_tokens=1, output_tokens=1),
        structured={"kind": kind, **extra},
    )


async def test_model_only_spots_words_the_runtime_normalises_them(api: ApiHandle) -> None:
    chat = Chat(api, script=[understood("answer", slots={"date": "depois de amanhã"})])
    await chat.say("Quero agendar um corte")
    out = await chat.say("melhor deixar para o dia posterior")  # rules do not parse it
    assert chat.flow.slots["date"] == "2026-10-07"  # computed from the reference date
    assert "Tenho estes horários para qua 07/10" in out.reply
    assert chat.llm.calls == 1


async def test_model_cannot_inject_a_value_the_parsers_reject(api: ApiHandle) -> None:
    chat = Chat(api, script=[understood("answer", slots={"date": "2099-13-45", "service": "x"})])
    await chat.say("Quero agendar um corte")
    out = await chat.say("sei lá, depois vejo")
    assert "date" not in chat.flow.slots
    assert out.reply.startswith("Não entendi.")


async def test_digression_is_answered_then_the_flow_resumes(api: ApiHandle) -> None:
    chat = Chat(
        api,
        script=[understood("digression"), text_response("Abrimos das 9h às 18h.")],
    )
    await chat.say("Quero agendar um corte")
    out = await chat.say("que horas vocês abrem?")
    assert out.reply == "Abrimos das 9h às 18h.\n\nPara qual dia você quer agendar?"
    assert chat.flow.slots == {"service": "haircut"} and chat.flow.digressions == 1
    out = await chat.say("amanhã")
    assert "Tenho estes horários" in out.reply  # back on track, nothing lost


async def test_a_digression_can_only_use_read_tools(api: ApiHandle) -> None:
    def forbidden(request: Any) -> LLMResponse:
        assert {t.name for t in request.tools} == {"scheduling__availability"}  # no create
        return tool_call_response(
            "scheduling__create",
            {
                "service_id": "haircut",
                "start_at": "2026-10-06T10:00:00-03:00",
                "duration_minutes": 30,
            },
        )

    chat = Chat(
        api, script=[understood("digression"), forbidden, text_response("Não posso agendar aqui.")]
    )
    await chat.say("Quero agendar um corte")
    out = await chat.say("agenda logo pra mim às 10")
    assert not out.proposed and not any(r["path"] == "/bookings" for r in api.requests)


# --- every canonical outcome leads somewhere safe (DESIGN 23.4) ---


def engine_returning(status: str, *, policy: PolicyGate | None = None) -> TurnEngine:
    error = None if status == "success" else ToolError(code="X", message_safe="x")
    provider = FakeToolProvider({"erp_get_available_slots": ToolResult(status=status, error=error)})
    engine, _, _ = build_engine(
        FakeLLM([]),
        api_base_url="http://unused",
        journal=new_journal(),
        clock=new_clock(),
        providers={"http": provider},
        policy=policy,
        flows=True,
    )
    return engine


OUTCOMES = {
    "validation_error": ("Não consegui usar essa data.", "open"),
    "business_error": ("Esse serviço não está disponível agora.", "closed"),
    "technical_error": ("Não consegui consultar a agenda agora.", "open"),
    "timeout": ("Não consegui consultar a agenda agora.", "open"),
    "unknown": ("Não consegui consultar a agenda agora.", "open"),
}


@pytest.mark.parametrize("status", sorted(OUTCOMES))
async def test_every_tool_outcome_has_a_transition_or_safe_default(status: str) -> None:
    expected, flow_state = OUTCOMES[status]
    outcome = await engine_returning(status).process_turn(
        IDENTITY, ConversationState(), "Quero marcar um corte amanhã", "t1"
    )
    assert expected in outcome.reply and not outcome.proposed
    assert (outcome.state.active_flow is None) == (flow_state == "closed")


async def test_policy_denied_follows_its_transition() -> None:
    engine = engine_returning("success", policy=PolicyGate(frozenset({"scheduling.create"})))
    outcome = await engine.process_turn(
        IDENTITY, ConversationState(), "Quero marcar um corte amanhã", "t1"
    )
    assert "Não posso consultar a agenda" in outcome.reply and outcome.state.flows == ()


def test_a_flow_cannot_be_defined_without_a_safe_default_or_with_dangling_references() -> None:
    from pydantic import ValidationError

    from conversation_agent.core.definitions.flow import Ask, Invoke, Say
    from conversation_agent.core.errors import DefinitionError

    inputs = {"service_id": FlowSlot(name="service")}
    with pytest.raises(ValidationError):  # `default` is mandatory
        Invoke(id="s", capability="scheduling.availability", inputs=inputs)  # type: ignore[call-arg]
    base = {"name": "f", "slots": PRICES.slots}
    unknown_outcome = Invoke(
        id="s", capability="c", inputs=inputs, on={"exploded": Say(text="x")}, default=Say(text="y")
    )
    with pytest.raises((DefinitionError, ValidationError)):
        FlowDefinition(steps=(unknown_outcome,), **base)  # type: ignore[arg-type]
    dangling = Invoke(
        id="s",
        capability="c",
        inputs=inputs,
        on={"timeout": Ask(slot="nope")},
        default=Say(text="y"),
    )
    with pytest.raises((DefinitionError, ValidationError)):
        FlowDefinition(steps=(dangling,), **base)  # type: ignore[arg-type]


def test_a_digression_may_only_use_read_capabilities() -> None:
    flow = SCHEDULING_FLOW.model_copy(update={"digression_capabilities": ("scheduling.create",)})
    agent = build_agent().model_copy(update={"flows": (flow,)})
    with pytest.raises(DefinitionError, match="digression"):
        agent._check_flows({c.name: c for c in agent.capabilities})


async def test_an_agent_compiled_from_its_manifest_behaves_like_the_python_one(
    api: ApiHandle,
) -> None:  # Phase 5: the manifest is a real definition, not documentation
    chat = Chat(api, from_manifest=True)
    out = await chat.say("Quero marcar um corte amanhã às 10h")
    assert proposed_start(out) == datetime(2026, 10, 6, 13, 0, tzinfo=UTC)
    out = await chat.say("na verdade às 11h")
    assert proposed_start(out) == datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
    assert len(api.availability_requests()) == 1


# --- a stated time is matched against EVERYTHING the tool returned, not just what fit on screen ---


async def test_a_stated_time_beyond_the_listed_options_is_still_found(api: ApiHandle) -> None:
    chat = Chat(api)
    out = await chat.say("Quero marcar um corte amanhã às 16h")  # the 12th option of 14 real ones
    assert proposed_start(out) == datetime(2026, 10, 6, 19, 0, tzinfo=UTC)  # 16:00 São Paulo
    assert "Não tenho exatamente" not in out.reply  # the system HAS that time free
    assert len(api.availability_requests()) == 1


async def test_a_time_said_while_choosing_can_pick_an_option_that_was_not_listed(
    api: ApiHandle,
) -> None:
    chat = Chat(api)
    shown = await chat.say("Quero agendar uma consulta amanhã")
    assert "5) ter 06/10 às 15:00" in shown.reply and "16:00" not in shown.reply
    out = await chat.say("pode ser às 16h")
    assert proposed_start(out) == datetime(2026, 10, 6, 19, 0, tzinfo=UTC)


def test_a_number_never_picks_an_option_that_was_not_listed() -> None:
    from datetime import date

    from conversation_agent.engine.flow_understanding import select_option

    def option(hour: str) -> dict[str, Any]:
        return {"value": hour, "label": hour, "local_date": "2026-10-06", "local_time": hour}

    shown = [option(f"0{h}:00") for h in range(1, 6)]
    hidden = [option("16:00")]
    today = date(2026, 10, 5)
    assert select_option("6", shown, today, unlisted=hidden) is None  # nothing was numbered 6
    assert select_option("o último", shown, today, unlisted=hidden) == shown[-1]
    assert select_option("às 16h", shown, today, unlisted=hidden) == hidden[0]  # a time can
