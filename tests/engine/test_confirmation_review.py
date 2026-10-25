"""`confirmation_review`: the runtime runs a read on a proposal and shows what it finds with the
confirmation question, whatever the model said."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from conversation_agent.adapters.llm.fake import FakeLLM, text_response, tool_call_response
from conversation_agent.adapters.tools.fake import FakeToolProvider
from conversation_agent.core.compiler import CompileError, compile_agent
from conversation_agent.core.definitions.binding import CapabilityBinding
from conversation_agent.core.definitions.capability import CapabilityDefinition
from conversation_agent.core.definitions.tool import HTTPRequestSpec, ToolDefinition
from conversation_agent.core.models.conversation import ConversationState
from conversation_agent.core.models.tooling import ToolError, ToolResult
from conversation_agent.engine.policy_gate import PolicyGate
from conversation_agent.engine.tool_runner import ToolRunner
from conversation_agent.engine.turn_engine import TurnEngine
from support.builders import IDENTITY, make_pipeline, new_clock, new_journal
from vertical_slice.definitions import (
    AVAILABILITY,
    AVAILABILITY_BINDING,
    CONNECTION,
    CREATE,
    CREATE_BINDING,
    ERP_CREATE_RESERVATION,
    ERP_GET_AVAILABLE_SLOTS,
    ErpFindArgs,
    build_agent,
)

CREATE_ARGS = {
    "service_id": "haircut",
    "start_at": "2026-10-06T10:00:00-03:00",
    "duration_minutes": 30,
}
ALL = frozenset({"scheduling.availability", "scheduling.create", "scheduling.check"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CheckInput(_Strict):
    service_id: str


class CheckOutput(_Strict):
    warnings: list[str]


class ErpCheckResponse(_Strict):
    notes: list[str]


CHECK = CapabilityDefinition(
    name="scheduling.check",
    description="Reviews a booking before it is proposed.",
    input_model=CheckInput,
    output_model=CheckOutput,
)
ERP_CHECK = ToolDefinition(
    name="erp_check",
    description="GET /check on the scheduling API.",
    input_model=ErpFindArgs,
    output_model=ErpCheckResponse,
    risk="read",
    provider="http",
    connection=CONNECTION,
    http=HTTPRequestSpec(method="GET", path="/check/{key}"),
)
CHECK_BINDING = CapabilityBinding(
    capability="scheduling.check",
    tool="erp_check",
    input_map={"key": "$.service_id"},
    output_map={"warnings": "$.notes"},
)
SLOTS = ToolResult(status="success", data={"items": [], "pagination": {"next_cursor": None}})


def agent_with_review(review: str | None = "scheduling.check", field: str = "warnings") -> Any:
    base = build_agent()
    create = CREATE.model_copy(
        update={"confirmation_review": review, "confirmation_review_field": field}
    )
    return base.model_copy(
        update={
            "capabilities": (AVAILABILITY, create, CHECK),
            "tools": (ERP_GET_AVAILABLE_SLOTS, ERP_CREATE_RESERVATION, ERP_CHECK),
            "bindings": (AVAILABILITY_BINDING, CREATE_BINDING, CHECK_BINDING),
        }
    )


def engine_for(llm: FakeLLM, check: ToolResult) -> tuple[TurnEngine, FakeToolProvider]:
    agent = agent_with_review()
    tools = FakeToolProvider(
        {"erp_get_available_slots": SLOTS, "erp_create_reservation": SLOTS, "erp_check": check}
    )
    pipeline = make_pipeline(agent, PolicyGate(ALL), ToolRunner({"http": tools}))
    return TurnEngine(compile_agent(agent), llm, pipeline, new_journal(), new_clock()), tools


async def turn(engine: TurnEngine) -> Any:
    return await engine.process_turn(IDENTITY, ConversationState(), "oi", "t1")


def propose_llm() -> FakeLLM:
    return FakeLLM([tool_call_response("scheduling__create", CREATE_ARGS), text_response("Ok.")])


async def test_the_review_findings_are_shown_with_the_confirmation_question() -> None:
    found = ToolResult(status="success", data={"notes": ["Horário apertado.", "  "]})
    engine, tools = engine_for(propose_llm(), found)
    outcome = await turn(engine)
    assert len(outcome.proposed) == 1
    assert outcome.proposed[0].notes == ("Horário apertado.",)
    assert "- Horário apertado.\n\nPosso confirmar?" in outcome.reply
    assert [c for c in tools.calls if c.tool_name == "erp_check"]  # the runtime ran the read


async def test_no_findings_leaves_the_question_as_it_was() -> None:
    engine, _ = engine_for(propose_llm(), ToolResult(status="success", data={"notes": []}))
    outcome = await turn(engine)
    assert outcome.proposed[0].notes == ()
    assert "- " not in outcome.reply and outcome.reply.rstrip().endswith("cancelar.")


async def test_a_failing_review_never_blocks_the_proposal() -> None:
    engine, _ = engine_for(
        propose_llm(),
        ToolResult(
            status="technical_error", error=ToolError(code="X", message_safe="down", retryable=True)
        ),
    )
    outcome = await turn(engine)
    assert len(outcome.proposed) == 1 and outcome.proposed[0].notes == ()
    assert "Posso confirmar?" in outcome.reply


def test_the_review_is_validated_and_leaves_the_digest_alone_when_unset() -> None:
    assert (
        compile_agent(agent_with_review()).digest != compile_agent(agent_with_review(None)).digest
    )
    for review, field, code in (
        ("nope.missing", "warnings", "REVIEW_UNKNOWN_CAPABILITY"),
        ("scheduling.check", "nope", "REVIEW_UNKNOWN_FIELD"),
        ("scheduling.availability", "slots", "REVIEW_UNKNOWN_INPUT"),
    ):
        with pytest.raises(CompileError, match=code):
            compile_agent(agent_with_review(review, field))
