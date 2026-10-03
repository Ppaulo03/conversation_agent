"""Live provider checks. Explicit profile only: `uv run --env-file .env pytest -m integration`.

The provider comes from the environment (LLM_PROVIDER / LLM_API_KEY / LLM_MODEL, or a legacy
GROQ_API_KEY / ANTHROPIC_API_KEY). Every call goes through a daily budget (see
tests/support/live_budget.py and tests/integration/conftest.py) because real providers have low
daily limits; the whole suite makes at most ~6 calls. Never part of the normal CI.
"""

from __future__ import annotations

import re

import httpx
import pytest

from conftest import ApiHandle
from contracts.llm_assertions import (
    assert_canonical_response,
    assert_text_response,
    assert_tool_call_response,
    simple_request,
)
from conversation_agent.core.models.conversation import ConversationState
from conversation_agent.core.models.llm import LLMToolDefinition
from conversation_agent.ports.llm import LLMProvider
from support.builders import IDENTITY, new_clock, new_journal
from vertical_slice.wiring import build_engine

pytestmark = pytest.mark.integration


async def test_live_provider_passes_the_same_shape_contract(live_llm: LLMProvider) -> None:
    assert_text_response(await live_llm.complete(simple_request("Reply with the single word OK.")))

    tool = LLMToolDefinition(
        name="get_weather",
        description="Get the weather for a city.",
        input_schema={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )
    request = simple_request("What's the weather in Lisbon? Use the tool.").model_copy(
        update={"tools": (tool,)}
    )
    assert_tool_call_response(await live_llm.complete(request), "get_weather")
    assert_canonical_response(await live_llm.complete(simple_request()))


async def test_live_llm_offers_only_slots_that_the_api_really_has(
    api: ApiHandle, live_llm: LLMProvider
) -> None:
    """Quality probe for the GO/NO-GO: real LLM + real API through the full pipeline."""
    engine, _, http = build_engine(
        live_llm, api_base_url=api.base_url, journal=new_journal(), clock=new_clock()
    )
    try:
        outcome = await engine.process_turn(
            IDENTITY,
            ConversationState(),
            "Oi, quero cortar o cabelo amanhã. Quais horários vocês têm?",
            "live-1",
        )
    finally:
        assert http is not None
        await http.aclose()

    real = {
        s["start"][11:16]
        for s in httpx.get(
            f"{api.base_url}/availability",
            params={"service_code": "HC-01", "start": "2026-10-06", "end": "2026-10-06"},
        ).json()["items"]
    }
    offered = set(re.findall(r"\b\d{2}:\d{2}\b", outcome.reply))
    assert api.availability_requests(), "the model never consulted availability"
    assert offered <= real, f"invented slots: {offered - real}"
