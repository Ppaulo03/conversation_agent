"""Phase 1 GO/NO-GO scenarios, end to end:

  user text -> TurnEngine -> LLM (scripted) -> Capability -> Binding -> HTTPToolProvider
  -> real HTTP -> reference-scheduling-api -> response

The LLM is scripted (deterministic CI), but each scripted step *reacts to what the engine
actually put in the tool result*, so data flowing from the real API through the capability
is what drives the replies. No scheduling logic exists in core/engine.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

import httpx

from conftest import ApiHandle
from conversation_agent.adapters.llm.fake import FakeLLM, text_response, tool_call_response
from conversation_agent.core.models.conversation import ConversationState, TurnOutcome
from conversation_agent.core.models.llm import LLMResponse
from support.builders import IDENTITY, SP, availability_call, new_clock, new_journal, react
from support.server import unused_port
from vertical_slice.definitions import CreateInput
from vertical_slice.wiring import build_engine


def hhmm(start_at: str) -> str:
    return datetime.fromisoformat(start_at).astimezone(SP).strftime("%H:%M")


def slot_times(result: dict[str, Any]) -> list[str]:
    return [hhmm(s["start_at"]) for s in result["data"]["slots"]]


def api_times(api: ApiHandle, service_code: str, day: str) -> list[str]:
    """Independent ground truth straight from the external API."""
    response = httpx.get(
        f"{api.base_url}/availability",
        params={"service_code": service_code, "start": day, "end": day},
    )
    return [hhmm(i["start"]) for i in response.json()["items"]]


class Conversation:
    def __init__(self, api_base_url: str) -> None:
        self.journal = new_journal()
        self.api_base_url = api_base_url
        self.state = ConversationState()
        self.n = 0
        self.llms: list[FakeLLM] = []

    async def say(self, text: str, script: list[Any]) -> TurnOutcome:
        llm = FakeLLM(script)
        self.llms.append(llm)
        engine, _, http = build_engine(
            llm, api_base_url=self.api_base_url, journal=self.journal, clock=new_clock()
        )
        self.n += 1
        try:
            outcome = await engine.process_turn(IDENTITY, self.state, text, f"turn-{self.n}")
        finally:
            assert http is not None
            await http.aclose()
        self.state = outcome.state
        return outcome


def offer_slots(result: dict[str, Any]) -> LLMResponse:
    times = slot_times(result)[:3]
    return text_response("Tenho estes horários: " + ", ".join(times))


# --- Cenário 1 — agendamento simples (multi-turn) ---------------------------------------


async def test_s1_simple_scheduling_conversation_is_multi_turn(api: ApiHandle) -> None:
    chat = Conversation(api.base_url)
    first = await chat.say(
        "Oi! Quero agendar um corte de cabelo.", [text_response("Claro! Para qual dia?")]
    )
    assert first.reply == "Claro! Para qual dia?"
    assert api.requests == []  # nothing to look up yet: no premature tool call

    second = await chat.say(
        "Amanhã.",
        [availability_call("haircut", "2026-10-06"), react(offer_slots)],
    )
    # The 2nd turn's LLM request carried the 1st turn (conversation continuity).
    texts = [
        p.text  # type: ignore[union-attr]
        for m in chat.llms[1].requests[0].messages
        for p in m.parts
        if hasattr(p, "text")
    ]
    assert texts[:2] == ["Oi! Quero agendar um corte de cabelo.", "Claro! Para qual dia?"]
    assert second.reply == "Tenho estes horários: 09:00, 09:30, 10:00"
    assert len(chat.state.history) == 4


# --- Cenário 2 — consulta e escolha de slots reais da API -------------------------------


async def test_s2_availability_comes_from_the_api_through_the_capability(api: ApiHandle) -> None:
    chat = Conversation(api.base_url)
    seen: dict[str, Any] = {}

    def capture(result: dict[str, Any]) -> LLMResponse:
        seen.update(result)
        return offer_slots(result)

    await chat.say(
        "Quero uma consulta na terça",
        [availability_call("consultation", "2026-10-06"), react(capture)],
    )
    # Binding input mapping reached the API in *its* vocabulary (service_id -> service_code).
    (request,) = api.availability_requests()
    assert request["query"] == {
        "service_code": "CN-01",
        "start": "2026-10-06",
        "end": "2026-10-06",
        "limit": "20",
    }
    # What the LLM saw is the capability schema, equal to the API's real data.
    assert set(seen["data"]) == {"slots", "next_cursor"}
    assert slot_times(seen) == api_times(api, "CN-01", "2026-10-06")
    assert seen["data"]["slots"][0].keys() == {"start_at", "end_at"}
    assert seen["status"] == "success"


async def test_s2_user_picks_one_of_the_real_slots(api: ApiHandle) -> None:
    chat = Conversation(api.base_url)
    await chat.say("terça", [availability_call("haircut", "2026-10-06"), react(offer_slots)])
    real = set(api_times(api, "HC-01", "2026-10-06"))
    offered = set(re.findall(r"\d\d:\d\d", chat.state.history[-1].text))
    assert offered and offered <= real


# --- Cenário 3 — indisponibilidade + alternativa sem inventar horário -------------------


async def test_s3_unavailable_day_leads_to_a_real_alternative(api: ApiHandle) -> None:
    api.state.fully_booked_dates = {date(2026, 10, 6)}
    chat = Conversation(api.base_url)

    def next_step(result: dict[str, Any]) -> LLMResponse:
        assert result["status"] == "success" and result["data"]["slots"] == []
        return availability_call("haircut", "2026-10-07")  # the agent looks for an alternative

    def offer_alternative(result: dict[str, Any]) -> LLMResponse:
        first = slot_times(result)[0]
        return text_response(f"Terça está lotada. Posso quarta às {first}?")

    outcome = await chat.say(
        "Quero cortar na terça",
        [availability_call("haircut", "2026-10-06"), react(next_step), react(offer_alternative)],
    )
    assert outcome.llm_calls == 3
    assert [r["query"]["start"] for r in api.availability_requests()] == [
        "2026-10-06",
        "2026-10-07",
    ]  # type: ignore[index]
    offered = re.findall(r"\d\d:\d\d", outcome.reply)
    assert offered == [api_times(api, "HC-01", "2026-10-07")[0]]  # a time the API really has


# --- Cenário 4 — correção de data/hora acompanha o estado -------------------------------


def propose(start_at: str) -> dict[str, Any]:
    return {"service_id": "haircut", "start_at": start_at, "duration_minutes": 30}


async def test_s4_user_correction_replaces_the_conversational_state(api: ApiHandle) -> None:
    chat = Conversation(api.base_url)
    await chat.say(
        "Corte na terça às 10h",
        [
            availability_call("haircut", "2026-10-06"),
            react(
                lambda r: tool_call_response(
                    "scheduling__create", propose("2026-10-06T10:00:00-03:00")
                )
            ),
            text_response("Proposta: terça 10h (ainda não agendado)."),
        ],
    )
    before = chat.state.proposals["scheduling.create"]
    assert before.args["start_at"] == "2026-10-06T13:00:00+00:00"

    await chat.say(
        "Na verdade quinta às 15h",
        [
            availability_call("haircut", "2026-10-08"),
            react(
                lambda r: tool_call_response(
                    "scheduling__create", propose("2026-10-08T15:00:00-03:00")
                )
            ),
            text_response("Proposta atualizada: quinta 15h (ainda não agendado)."),
        ],
    )
    after = chat.state.proposals["scheduling.create"]
    assert after.args["start_at"] == "2026-10-08T18:00:00+00:00"
    assert after.args_hash != before.args_hash  # stale proposal is superseded, not reused
    assert len(chat.state.proposals) == 1
    assert [r["query"]["start"] for r in api.availability_requests()] == [
        "2026-10-06",
        "2026-10-08",
    ]  # type: ignore[index]


# --- Cenário 5 — falha HTTP: sem sucesso nem disponibilidade inventados -----------------


def honest_reply(result: dict[str, Any]) -> LLMResponse:
    if result["status"] == "success":
        return offer_slots(result)
    return text_response("Não consegui consultar os horários agora. Tente de novo mais tarde.")


async def test_s5_http_5xx_does_not_invent_success_or_availability(api: ApiHandle) -> None:
    api.state.fault = {"status": 503}
    chat = Conversation(api.base_url)
    seen: dict[str, Any] = {}

    def capture(result: dict[str, Any]) -> LLMResponse:
        seen.update(result)
        return honest_reply(result)

    outcome = await chat.say(
        "Quero cortar amanhã", [availability_call("haircut", "2026-10-06"), react(capture)]
    )
    assert seen["status"] == "technical_error"
    assert "data" not in seen and seen["error"]["retryable"] is True
    assert "injected fault" not in json.dumps(seen)  # raw body never reaches the LLM
    assert not re.findall(r"\d\d:\d\d", outcome.reply)
    assert chat.state.proposals == {}


async def test_s5_unreachable_api_does_not_invent_success_or_availability(api: ApiHandle) -> None:
    chat = Conversation(f"http://127.0.0.1:{unused_port()}")
    seen: dict[str, Any] = {}

    def capture(result: dict[str, Any]) -> LLMResponse:
        seen.update(result)
        return honest_reply(result)

    outcome = await chat.say(
        "Quero cortar amanhã", [availability_call("haircut", "2026-10-06"), react(capture)]
    )
    assert seen["status"] == "technical_error" and "data" not in seen
    assert not re.findall(r"\d\d:\d\d", outcome.reply)


async def test_s5_business_error_is_surfaced_with_its_code(api: ApiHandle) -> None:
    """API 404 SERVICE_NOT_FOUND -> binding error_map -> canonical business_error."""
    from conversation_agent.adapters.tools.http import HTTPConnection, HTTPToolProvider
    from vertical_slice.definitions import CONNECTION

    provider = HTTPToolProvider({CONNECTION: HTTPConnection(base_url=api.base_url)})
    api.state.fault = None
    llm = FakeLLM([availability_call("haircut", "2026-10-06"), text_response("ok")])
    engine, _, _ = build_engine(
        llm, api_base_url="", journal=new_journal(), clock=new_clock(), providers={"http": provider}
    )
    # Force a 404 by making the API reject the translated service code.
    api.state.fault = {"status": 404}
    await engine.process_turn(IDENTITY, ConversationState(), "oi", "t-404")
    seen = json.loads(
        next(p.content for m in llm.requests[1].messages for p in m.parts if hasattr(p, "content"))  # type: ignore[union-attr]
    )
    assert seen["status"] == "business_error"
    assert seen["error"]["code"] == "SERVICE_NOT_FOUND"
    await provider.aclose()


# --- Cenário 6 — chega ao draft válido de scheduling.create sem executá-lo --------------


async def test_s6_conversation_reaches_a_valid_create_draft_without_executing(
    api: ApiHandle,
) -> None:
    chat = Conversation(api.base_url)
    await chat.say(
        "Quero corte amanhã", [availability_call("haircut", "2026-10-06"), react(offer_slots)]
    )
    outcome = await chat.say(
        "Pode ser às 10h",
        [
            tool_call_response("scheduling__create", propose("2026-10-06T10:00:00-03:00")),
            text_response("Proposta pronta: terça 10:00. O agendamento ainda não foi efetivado."),
        ],
    )
    draft = chat.state.proposals["scheduling.create"]
    # A valid, canonical CapabilityRequest for the future protected action:
    parsed = CreateInput.model_validate(draft.args)
    assert (parsed.service_id, parsed.duration_minutes) == ("haircut", 30)
    assert parsed.start_at.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
    assert len(draft.args_hash) == 64
    # ...and nothing was executed: the API only ever saw availability reads.
    assert {(r["path"]) for r in api.requests} == {"/availability"}
    assert "ainda não foi efetivado" in outcome.reply
