"""Phase 4: Flows. Deterministic conversation on top of the real capability path.

No LLM is scripted anywhere in this file: every turn is answered by the Flow, and each test
asserts the model was never called. The scheduling API is the real reference service.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from conftest import ApiHandle
from conversation_agent.adapters.llm.fake import (
    FakeLLM,
    Script,
    text_response,
    tool_call_response,
)
from conversation_agent.core.definitions.flow import Collect, FlowDefinition, SlotDefinition
from conversation_agent.core.models.conversation import ConversationState, TurnOutcome
from conversation_agent.core.models.llm import LLMResponse, LLMStopReason, LLMUsage
from conversation_agent.engine.turn_engine import TurnEngine
from support.builders import IDENTITY, new_clock, new_journal
from vertical_slice.definitions import SCHEDULING_FLOW, build_agent
from vertical_slice.wiring import build_engine

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
        self, api: ApiHandle, *, extra_flows: bool = False, script: list[Script] | None = None
    ) -> None:
        self.strict = script is None
        self.llm = FakeLLM(script or [])  # strict chats fail on ANY model call
        self.state = ConversationState()
        self.journal = new_journal()
        self.n = 0
        if extra_flows:
            engine, agent, _ = build_engine(
                self.llm,
                api_base_url=api.base_url,
                journal=self.journal,
                clock=new_clock(),
                flows=True,
            )
            agent = agent.model_copy(update={"flows": (SCHEDULING_FLOW, PRICES)})
            engine = TurnEngine(
                agent,
                self.llm,
                engine._pipeline,
                self.journal,
                new_clock(),
            )
        else:
            engine, _, _ = build_engine(
                self.llm,
                api_base_url=api.base_url,
                journal=self.journal,
                clock=new_clock(),
                flows=True,
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
