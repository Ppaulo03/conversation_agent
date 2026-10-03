"""Live conversational scenarios (real LLM + real HTTP API), as a manual quality gate.

These complement the deterministic GO/NO-GO suite, which proves "if the model chooses
correctly, the runtime does the right thing". Here we check that a real model does choose
correctly often enough. Assertions are deliberately tolerant of wording and focus on
behaviour: tools consulted, no invented slots, no write executed, draft well-formed.

Each test costs a few LLM calls and goes through the daily budget; run them sparingly:
  uv run --env-file .env pytest -m integration -k live_s3
"""

from __future__ import annotations

import re
from datetime import date

import httpx
import pytest

from conftest import ApiHandle
from conversation_agent.core.models.conversation import ConversationState
from conversation_agent.ports.llm import LLMProvider
from support.builders import IDENTITY, new_clock, new_journal
from vertical_slice.definitions import CreateInput
from vertical_slice.wiring import build_engine

pytestmark = pytest.mark.integration


class LiveChat:
    def __init__(self, llm: LLMProvider, api: ApiHandle) -> None:
        self.state = ConversationState()
        self.api = api
        self.journal = new_journal()
        self.llm = llm
        self.turn = 0

    async def say(self, text: str) -> str:
        engine, _, http = build_engine(
            self.llm, api_base_url=self.api.base_url, journal=self.journal, clock=new_clock()
        )
        self.turn += 1
        try:
            outcome = await engine.process_turn(IDENTITY, self.state, text, f"live-{self.turn}")
        finally:
            assert http is not None
            await http.aclose()
        self.state = outcome.state
        assert outcome.halted is None, f"turn halted: {outcome.halted}"
        return outcome.reply


def real_times(api: ApiHandle) -> set[str]:
    """Every HH:MM the API can ever offer for a haircut (ground truth, no LLM involved)."""
    items = httpx.get(
        f"{api.base_url}/availability",
        params={"service_code": "HC-01", "start": "2026-10-05", "end": "2026-10-16", "limit": 50},
    ).json()["items"]
    return {i["start"][11:16] for i in items}


def only_reads_happened(api: ApiHandle) -> bool:
    return {(r["path"]) for r in api.requests} <= {"/availability"}


async def test_live_s3_unavailable_day_offers_a_real_alternative(
    api: ApiHandle, live_llm: LLMProvider
) -> None:
    api.state.fully_booked_dates = {date(2026, 10, 6)}  # Tuesday has no slots
    chat = LiveChat(live_llm, api)
    reply = await chat.say(
        "Oi! Quero cortar o cabelo na terça-feira (dia 6/10). Tem horário? "
        "Se não tiver, me sugira outro dia."
    )
    queried = {r["query"]["start"] for r in api.availability_requests()}  # type: ignore[index]
    assert queried, "the model never consulted availability"
    offered = set(re.findall(r"\b\d{1,2}:\d{2}\b", reply))
    assert {t.zfill(5) for t in offered} <= real_times(api), f"invented slots in: {reply!r}"
    assert only_reads_happened(api)


async def test_live_s6_choosing_a_slot_yields_a_valid_create_draft(
    api: ApiHandle, live_llm: LLMProvider
) -> None:
    chat = LiveChat(live_llm, api)
    await chat.say("Oi! Quero cortar o cabelo amanhã. Quais horários tem?")
    await chat.say("Pode ser às 10:00, por favor.")
    draft = chat.state.proposals.get("scheduling.create")
    assert draft is not None, "the model never recorded the create proposal"
    parsed = CreateInput.model_validate(draft.args)
    assert parsed.service_id == "haircut"
    assert parsed.duration_minutes == 30
    assert parsed.start_at.isoformat() == "2026-10-06T13:00:00+00:00"  # 10:00 America/Sao_Paulo
    assert only_reads_happened(api), "a write reached the external system"


async def test_live_s4_user_correction_replaces_the_proposal(
    api: ApiHandle, live_llm: LLMProvider
) -> None:
    chat = LiveChat(live_llm, api)
    await chat.say("Quero cortar o cabelo na terça-feira (6/10) às 10:00.")
    first = chat.state.proposals.get("scheduling.create")
    await chat.say("Na verdade prefiro quinta-feira (8/10) às 15:00.")
    final = chat.state.proposals.get("scheduling.create")
    assert first is not None and final is not None
    assert final.args["start_at"] == "2026-10-08T18:00:00+00:00"  # 15:00 America/Sao_Paulo
    assert final.args_hash != first.args_hash
    assert only_reads_happened(api)
