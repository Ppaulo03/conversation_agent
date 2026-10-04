"""PolicyGate (allowlists, ownership, budgets, loops, limits, schedule) and operational
guardrails (token budget, result truncation, redaction). DESIGN §11, §11.1."""

from __future__ import annotations

import json
from typing import Any

import pytest

from conversation_agent.adapters.llm.fake import FakeLLM, text_response, tool_call_response
from conversation_agent.adapters.tools.fake import FakeToolProvider
from conversation_agent.core.models.conversation import ConversationState
from conversation_agent.core.models.llm import (
    LLMResponse,
    LLMStopReason,
    LLMUsage,
    TextPart,
    ToolResultPart,
)
from conversation_agent.core.models.runtime import Ownership
from conversation_agent.core.models.tooling import CapabilityResult, ToolResult
from conversation_agent.core.redaction import redact
from conversation_agent.engine.capability_pipeline import CapabilityPipeline
from conversation_agent.engine.policy_gate import PolicyGate
from conversation_agent.engine.policy_rules import (
    AllowedHoursRule,
    LoopRule,
    NumericLimitRule,
    OwnershipRule,
    PolicyContext,
    ToolCallBudgetRule,
)
from conversation_agent.engine.prompts import render_result
from conversation_agent.engine.tool_runner import ToolRunner
from conversation_agent.engine.turn_engine import TurnEngine
from support.builders import IDENTITY, availability_call, new_clock, new_journal
from vertical_slice.definitions import build_agent

ALL = frozenset({"scheduling.availability", "scheduling.create"})
SLOTS = ToolResult(status="success", data={"items": [], "pagination": {"next_cursor": None}})
CREATE_ARGS = {
    "service_id": "haircut",
    "start_at": "2026-10-06T10:00:00-03:00",
    "duration_minutes": 30,
}


def engine_for(
    llm: FakeLLM, gate: PolicyGate, **agent_updates: Any
) -> tuple[TurnEngine, FakeToolProvider]:
    agent = build_agent().model_copy(update=agent_updates)
    tools = FakeToolProvider({"erp_get_available_slots": SLOTS, "erp_create_reservation": SLOTS})
    pipeline = CapabilityPipeline(agent, gate, ToolRunner({"http": tools}))
    return TurnEngine(agent, llm, pipeline, new_journal(), new_clock()), tools


def seen_results(llm: FakeLLM) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for request in llm.requests:
        for m in request.messages:
            for p in m.parts:
                if isinstance(p, ToolResultPart):
                    out.append(json.loads(p.content))
    return out


async def turn(engine: TurnEngine) -> Any:
    return await engine.process_turn(IDENTITY, ConversationState(), "oi", "t1")


# --- the gate itself ---


def test_a_tenant_allowlist_can_only_narrow_never_widen() -> None:
    gate = PolicyGate(
        frozenset({"scheduling.availability"}),
        tenant_allowlists={"acme": frozenset({"scheduling.availability", "scheduling.create"})},
    )
    assert gate.allowed_for("acme") == frozenset({"scheduling.availability"})  # no widening
    narrow = PolicyGate(ALL, tenant_allowlists={"acme": frozenset({"scheduling.availability"})})
    assert narrow.allowed_for("acme") == frozenset({"scheduling.availability"})
    assert narrow.allowed_for("other") == ALL


def test_the_ownership_rule_denies_any_call_when_a_human_owns_the_conversation() -> None:  # INV-019
    agent = build_agent()
    resolved = agent.resolve("scheduling.availability")
    gate = PolicyGate(ALL, rules=[OwnershipRule()])
    for owner in (Ownership.HUMAN, Ownership.HANDOFF_PENDING):
        decision = gate.evaluate(
            "scheduling.availability", resolved, PolicyContext(ownership=owner)
        )
        assert (decision.outcome, decision.reason) == ("deny", "conversation_not_bot_owned")
    assert gate.evaluate("scheduling.availability", resolved, PolicyContext()).outcome == "allow"


def test_effective_risk_decides_protection_not_the_prompt() -> None:
    agent = build_agent()
    gate = PolicyGate(ALL, rules=[])
    read = gate.evaluate("scheduling.availability", agent.resolve("scheduling.availability"))
    write = gate.evaluate("scheduling.create", agent.resolve("scheduling.create"))
    assert read.outcome == "allow" and write.outcome == "require_confirmation"


# --- rules inside a turn ---


async def test_a_tool_call_budget_stops_runaway_calls() -> None:
    llm = FakeLLM(
        [availability_call("haircut", f"2026-10-0{d}") for d in (6, 7, 8)] + [text_response("fim")]
    )
    gate = PolicyGate(ALL, rules=[ToolCallBudgetRule(2)])
    engine, tools = engine_for(llm, gate)
    await turn(engine)
    assert len(tools.calls) == 2  # the third call never reached the provider
    assert seen_results(llm)[-1]["error"]["code"] == "TOOL_CALL_BUDGET_EXCEEDED"


async def test_a_repeated_identical_call_is_a_loop_and_is_denied() -> None:
    same = availability_call("haircut", "2026-10-06")
    llm = FakeLLM([same, same, same, same, text_response("desisto")])
    engine, tools = engine_for(llm, PolicyGate(ALL, rules=[LoopRule(3)]))
    await turn(engine)
    assert len(tools.calls) == 3
    assert seen_results(llm)[-1]["error"]["code"] == "LOOP_DETECTED"


async def test_a_numeric_limit_forbids_the_action_before_it_is_even_proposed() -> None:
    too_long = tool_call_response("scheduling__create", {**CREATE_ARGS, "duration_minutes": 90})
    llm = FakeLLM([too_long, text_response("Não posso agendar isso.")])
    gate = PolicyGate(ALL, rules=[NumericLimitRule("scheduling.create", "duration_minutes", 60)])
    engine, tools = engine_for(llm, gate)
    outcome = await turn(engine)
    assert outcome.proposed == () and outcome.state.proposals == {}  # no PendingAction to confirm
    assert tools.calls == []
    assert seen_results(llm)[-1]["error"]["code"] == "LIMIT_EXCEEDED:DURATION_MINUTES"


async def test_a_schedule_rule_denies_outside_the_allowed_hours() -> None:
    llm = FakeLLM([tool_call_response("scheduling__create", CREATE_ARGS), text_response("ok")])
    rule = AllowedHoursRule("scheduling.create", 9, 18, "America/Sao_Paulo")  # clock says 08:00
    engine, _ = engine_for(llm, PolicyGate(ALL, rules=[rule]))
    outcome = await turn(engine)
    assert outcome.proposed == ()
    assert seen_results(llm)[-1]["error"]["code"] == "OUTSIDE_ALLOWED_HOURS"


async def test_the_default_gate_still_holds_writes_for_confirmation() -> None:
    llm = FakeLLM([tool_call_response("scheduling__create", CREATE_ARGS), text_response("Resumo.")])
    engine, tools = engine_for(llm, PolicyGate(ALL))
    outcome = await turn(engine)
    assert len(outcome.proposed) == 1 and tools.calls == []
    assert "Posso confirmar?" in outcome.reply  # the runtime-owned confirmation question


# --- guardrails ---


def costly(tool_call: bool) -> LLMResponse:
    parts: tuple[Any, ...] = (
        (availability_call("haircut", "2026-10-06").parts) if tool_call else (TextPart(text="ok"),)
    )
    return LLMResponse(
        parts=parts,
        stop_reason=LLMStopReason.TOOL_USE if tool_call else LLMStopReason.END_TURN,
        usage=LLMUsage(input_tokens=400, output_tokens=200),
    )


async def test_the_token_budget_halts_the_turn_before_more_tools_run() -> None:
    llm = FakeLLM([costly(True), costly(True), text_response("x")])
    engine, tools = engine_for(llm, PolicyGate(ALL), max_tokens_per_turn=500)
    outcome = await turn(engine)
    assert outcome.halted == "token_budget"
    assert tools.calls == []  # the very first response already exceeded the budget (600 > 500)
    assert outcome.reply == build_agent().fallback_reply


def test_oversized_tool_results_are_replaced_by_a_marker() -> None:
    big = CapabilityResult(status="success", data={"blob": "x" * 50_000})
    rendered = json.loads(render_result(big, max_chars=1000))
    assert rendered["data_truncated"] is True and "data" not in rendered
    assert len(rendered["data_preview"]) == 1000
    small = json.loads(render_result(CapabilityResult(status="success", data={"a": 1})))
    assert small["data"] == {"a": 1} and "data_truncated" not in small


@pytest.mark.parametrize(
    ("raw", "must_not_contain"),
    [
        ("Authorization: Bearer abcdef1234567890xyz", "abcdef1234567890xyz"),
        ("key sk-ant-api03-ABCDEFGHIJKLMNOP leaked", "ABCDEFGHIJKLMNOP"),
        ("api_key=supersecretvalue123", "supersecretvalue123"),
        ("password: hunter2hunter2", "hunter2hunter2"),
        ("contato joao.silva@example.com agora", "joao.silva@example.com"),
        ("CPF 123.456.789-09 do cliente", "123.456.789-09"),
        ("CNPJ 12.345.678/0001-95", "12.345.678/0001-95"),
        ("ligue +55 (11) 91234-5678 já", "91234-5678"),
    ],
)
def test_logs_never_carry_secrets_or_common_pii(raw: str, must_not_contain: str) -> None:
    cleaned = redact(raw)
    assert must_not_contain not in cleaned and "REDACTED" in cleaned


def test_redaction_leaves_ordinary_text_alone() -> None:
    assert redact("Tenho horários às 09:00 e 09:30") == "Tenho horários às 09:00 e 09:30"
