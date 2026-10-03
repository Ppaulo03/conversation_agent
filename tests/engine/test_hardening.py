"""Phase 1.1 hardening: pre-I/O vs post-I/O classification, stop_reason, adapter lifecycle."""

from __future__ import annotations

import json
from typing import Any

import pytest

from conversation_agent.adapters.llm.anthropic import AnthropicLLM
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.tools.fake import FakeToolProvider
from conversation_agent.app.llm_factory import close_llm
from conversation_agent.core.models.conversation import ConversationState
from conversation_agent.core.models.llm import (
    LLMResponse,
    LLMStopReason,
    LLMUsage,
    TextPart,
    ToolResultPart,
)
from conversation_agent.core.models.tooling import ToolContext, ToolResult
from conversation_agent.engine.tool_runner import ToolRunner
from conversation_agent.tools.requests import build_capability_request
from support.builders import IDENTITY, availability_call, new_clock, new_journal
from vertical_slice.definitions import AVAILABILITY, CREATE, build_agent
from vertical_slice.wiring import build_engine

CONTEXT = ToolContext(
    tenant_id="t",
    agent_id="a",
    agent_version="1",
    channel_id="c",
    conversation_id="cv",
    session_id="s",
    contact_id="p",
    turn_id="turn",
    invocation_id="inv",
    trace_id="tr",
)
CREATE_ARGS = {
    "service_id": "haircut",
    "start_at": "2026-10-06T10:00:00-03:00",
    "duration_minutes": 30,
}


# --- pre-I/O failures are technical_error, post-I/O ambiguity is unknown ------------------


@pytest.mark.parametrize("name", ["scheduling.availability", "scheduling.create"])
async def test_missing_provider_is_technical_error_even_for_writes(name: str) -> None:
    """Nothing was attempted, so reconciliation ('maybe it executed') would be wrong."""
    resolved = build_agent().resolve(name)
    assert resolved is not None
    capability = AVAILABILITY if name.endswith("availability") else CREATE
    args = (
        {"service_id": "haircut", "from_date": "2026-10-06", "to_date": "2026-10-06"}
        if capability is AVAILABILITY
        else CREATE_ARGS
    )
    result = await ToolRunner({}).run(resolved, build_capability_request(capability, args), CONTEXT)
    assert result.status == "technical_error"
    assert result.error is not None and result.error.code == "PROVIDER_NOT_CONFIGURED"


async def test_provider_crash_on_a_write_stays_unknown() -> None:
    class Exploding:
        async def execute(self, *_: Any, **__: Any) -> ToolResult:
            raise RuntimeError("boom")

    resolved = build_agent().resolve("scheduling.create")
    assert resolved is not None
    result = await ToolRunner({"http": Exploding()}).run(
        resolved, build_capability_request(CREATE, CREATE_ARGS), CONTEXT
    )
    assert result.status == "unknown"


async def test_binding_input_defect_is_technical_error_before_io() -> None:
    resolved = build_agent().resolve("scheduling.create")
    assert resolved is not None
    broken = resolved.model_copy(
        update={"binding": resolved.binding.model_copy(update={"input_map": {"x": "$.nope"}})}
    )
    provider = FakeToolProvider({})
    result = await ToolRunner({"http": provider}).run(
        broken, build_capability_request(CREATE, CREATE_ARGS), CONTEXT
    )
    assert result.status == "technical_error"
    assert provider.calls == []


# --- stop_reason ---------------------------------------------------------------------------


def truncated(text: str = "Tenho os seguintes horár") -> LLMResponse:
    return LLMResponse(
        parts=(TextPart(text=text),),
        stop_reason=LLMStopReason.MAX_TOKENS,
        usage=LLMUsage(input_tokens=1, output_tokens=1),
    )


def make_engine(llm: FakeLLM, provider: FakeToolProvider):  # type: ignore[no-untyped-def]
    engine, _, _ = build_engine(
        llm,
        api_base_url="http://unused",
        journal=new_journal(),
        clock=new_clock(),
        providers={"http": provider},
    )
    return engine


async def test_truncated_llm_output_is_not_delivered_as_a_final_answer() -> None:
    llm = FakeLLM([truncated()])
    outcome = await make_engine(llm, FakeToolProvider({})).process_turn(
        IDENTITY, ConversationState(), "oi", "t1"
    )
    assert outcome.halted == "llm_truncated"
    assert outcome.reply == build_agent().fallback_reply
    assert "horár" not in outcome.reply


async def test_truncated_response_with_tool_call_does_not_execute_the_tool() -> None:
    """Arguments of a cut-off tool call cannot be trusted."""
    cut = availability_call("haircut", "2026-10-06").model_copy(
        update={"stop_reason": LLMStopReason.MAX_TOKENS}
    )
    provider = FakeToolProvider({})
    outcome = await make_engine(FakeLLM([cut]), provider).process_turn(
        IDENTITY, ConversationState(), "oi", "t1"
    )
    assert outcome.halted == "llm_truncated"
    assert provider.calls == []


async def test_end_turn_and_tool_use_still_work_normally() -> None:
    ok = ToolResult(status="success", data={"items": [], "pagination": {"next_cursor": None}})
    provider = FakeToolProvider({"erp_get_available_slots": ok})
    llm = FakeLLM([availability_call("haircut", "2026-10-06"), text_response("Sem horários.")])
    outcome = await make_engine(llm, provider).process_turn(
        IDENTITY, ConversationState(), "oi", "t1"
    )
    assert outcome.halted is None and outcome.reply == "Sem horários."
    seen = [p for m in llm.requests[1].messages for p in m.parts if isinstance(p, ToolResultPart)]
    assert json.loads(seen[0].content)["status"] == "success"


async def test_truncation_is_journaled_and_replays_identically() -> None:
    journal = new_journal()
    engine, _, _ = build_engine(
        FakeLLM([truncated()]),
        api_base_url="x",
        journal=journal,
        clock=new_clock(),
        providers={"http": FakeToolProvider({})},
    )
    first = await engine.process_turn(IDENTITY, ConversationState(), "oi", "t1")
    replay_llm = FakeLLM([])
    engine2, _, _ = build_engine(
        replay_llm,
        api_base_url="x",
        journal=journal,
        clock=new_clock(),
        providers={"http": FakeToolProvider({})},
    )
    again = await engine2.process_turn(IDENTITY, ConversationState(), "oi", "t1")
    assert replay_llm.calls == 0 and again.halted == first.halted == "llm_truncated"


# --- adapter lifecycle -----------------------------------------------------------------------


async def test_anthropic_adapter_closes_its_client() -> None:
    class Client:
        closed = False
        messages: Any = None

        async def close(self) -> None:
            self.closed = True

    client = Client()
    llm = AnthropicLLM(client, model="m")
    await close_llm(llm)
    assert client.closed
