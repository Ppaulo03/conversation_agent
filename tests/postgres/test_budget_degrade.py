"""Phase 14.2: a tenant over its LLM budget is TOLD, not dropped (policy `degrade`, the default).

Messages are accepted and stored as always. A turn that would start new work is not run through the
model: the contact gets one fixed notice per exceeded period and later messages in that period are
recorded but not answered. Work under way finishes normally. `alert` and `refuse_new` are unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from conftest import ApiHandle
from conversation_agent.adapters.clock import SystemClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response, tool_call_response
from conversation_agent.adapters.manifest.yaml_loader import load_manifest_file
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.adapters.postgres.llm_budget import BudgetEvaluator, PostgresBudgetStore
from conversation_agent.adapters.postgres.llm_usage import PostgresLLMUsageStore
from conversation_agent.adapters.senders.console import ConsoleChannel
from conversation_agent.adapters.tools.http import HTTPToolProvider, local_dev_connection
from conversation_agent.app.runtime import Runtime
from conversation_agent.core.compiler import CompiledAgent, compile_manifest
from conversation_agent.core.llm_budget import DEFAULT_DEGRADED_REPLY, LLMBudget
from conversation_agent.core.llm_prices import PriceTable
from conversation_agent.core.models.llm_usage import LLMCallRecord
from postgres.test_runtime_paths import compiled, identity
from vertical_slice.definitions import CONNECTION
from vertical_slice.wiring import MANIFEST_PATH

TENANT = "tenant-1"
BOOKING_ARGS = {
    "service_id": "haircut",
    "start_at": "2026-10-06T10:00:00-03:00",
    "duration_minutes": 30,
}
CLOCK = SystemClock("America/Sao_Paulo")


async def over_budget(db: PostgresDatabase, on_exceed: str | None = None) -> BudgetEvaluator:
    budgets = PostgresBudgetStore(db)
    fields = {"daily_tokens": 100}
    if on_exceed is not None:
        fields["on_exceed"] = on_exceed
    await budgets.set(TENANT, LLMBudget.model_validate(fields), actor="ops")
    usage = PostgresLLMUsageStore(db)
    await usage.record(
        LLMCallRecord(
            tenant_id=TENANT,
            started_at=datetime.now(UTC),
            provider="p",
            model="m",
            input_tokens=900,
            output_tokens=100,
        )
    )
    return BudgetEvaluator(budgets, usage, PriceTable(), ttl_seconds=0)


def runtime_with(
    db: PostgresDatabase,
    api: ApiHandle,
    llm: FakeLLM,
    evaluator: BudgetEvaluator | None,
    agent: CompiledAgent | None = None,
) -> tuple[Runtime, ConsoleChannel, list[str]]:
    out: list[str] = []
    channel = ConsoleChannel(identity("b"), CLOCK, write=out.append)
    runtime = Runtime.build(
        db=db,
        compiled=agent or compiled(),
        llm=llm,
        providers={
            "http": HTTPToolProvider.static({CONNECTION: local_dev_connection(api.base_url)})
        },
        sender=channel,
        clock=CLOCK,
        budget=evaluator,
    )
    return runtime, channel, out


async def say(runtime: Runtime, channel: ConsoleChannel, text: str) -> None:
    await runtime.receive(channel.inbound(text))
    await runtime.drain()


async def test_the_default_policy_is_degrade() -> None:
    assert LLMBudget(daily_tokens=1).on_exceed == "degrade"
    # a budget stored before the default existed keeps what it said
    assert LLMBudget.model_validate({"daily_tokens": 1, "on_exceed": "alert"}).on_exceed == "alert"


async def test_over_the_budget_the_contact_is_told_once_and_the_model_is_not_called(
    db: PostgresDatabase, api: ApiHandle
) -> None:
    llm = FakeLLM([])  # any model call would fail the test
    runtime, channel, out = runtime_with(db, api, llm, await over_budget(db))

    await say(runtime, channel, "oi")
    assert out == [f"bot> {DEFAULT_DEGRADED_REPLY}"]  # said, in the framework's default wording
    await say(runtime, channel, "alguém aí?")
    await say(runtime, channel, "preciso de ajuda")
    assert (
        len(out) == 1 and llm.calls == 0
    )  # once per period: later messages are kept, not answered

    inbox = await db.pool.fetch("SELECT status FROM inbox_events")
    assert {r["status"] for r in inbox} == {"CONSUMED"}  # nothing is left waiting, nothing was lost
    assert await db.pool.fetchval("SELECT count(*) FROM turns WHERE status = 'COMPLETED'") == 3


async def test_the_notice_is_in_the_agents_own_words() -> None:
    raw = load_manifest_file(MANIFEST_PATH)
    raw["budget_exceeded_reply"] = "Estamos com muitas mensagens agora. Tente de novo mais tarde."
    agent = compile_manifest(raw)
    assert agent.agent.budget_exceeded_reply and agent.digest != compiled().digest


async def test_the_agents_wording_is_what_the_contact_reads(
    db: PostgresDatabase, api: ApiHandle
) -> None:
    raw = load_manifest_file(MANIFEST_PATH)
    raw["budget_exceeded_reply"] = "Estamos com muitas mensagens agora. Tente de novo mais tarde."
    runtime, channel, out = runtime_with(
        db, api, FakeLLM([]), await over_budget(db), compile_manifest(raw)
    )
    await say(runtime, channel, "oi")
    assert out == ["bot> Estamos com muitas mensagens agora. Tente de novo mais tarde."]


async def test_a_new_exceeded_period_gets_its_own_notice(
    db: PostgresDatabase, api: ApiHandle
) -> None:
    runtime, channel, out = runtime_with(db, api, FakeLLM([]), await over_budget(db))
    await say(runtime, channel, "oi")
    await db.pool.execute(
        "UPDATE conversation_states SET state_json = jsonb_set(state_json, '{budget_notice}', "
        "'\"2020-01-01T00:00:00+00:00\"')"
    )  # the contact was last told about an OLD period
    await say(runtime, channel, "voltei")
    assert len(out) == 2


@pytest.mark.parametrize("policy", ["alert", "refuse_new"])
async def test_the_other_policies_do_not_degrade_the_turn(
    db: PostgresDatabase, api: ApiHandle, policy: str
) -> None:  # refuse_new acts at the edge, alert only reports: the coordinator serves the turn
    llm = FakeLLM([text_response("Olá!")])
    runtime, channel, out = runtime_with(db, api, llm, await over_budget(db, policy))
    await say(runtime, channel, "oi")
    assert out == ["bot> Olá!"] and llm.calls == 1


async def test_a_tenant_within_its_budget_is_served_normally(
    db: PostgresDatabase, api: ApiHandle
) -> None:
    budgets = PostgresBudgetStore(db)
    await budgets.set(TENANT, LLMBudget(daily_tokens=1_000_000), actor="ops")
    evaluator = BudgetEvaluator(budgets, PostgresLLMUsageStore(db), PriceTable(), ttl_seconds=0)
    runtime, channel, out = runtime_with(db, api, FakeLLM([text_response("Olá!")]), evaluator)
    await say(runtime, channel, "oi")
    assert out == ["bot> Olá!"]


async def test_without_a_budget_gate_nothing_changes(db: PostgresDatabase, api: ApiHandle) -> None:
    runtime, channel, out = runtime_with(db, api, FakeLLM([text_response("Olá!")]), None)
    await say(runtime, channel, "oi")
    assert out == ["bot> Olá!"]


async def test_a_confirmation_already_under_way_is_never_cut_off(
    db: PostgresDatabase, api: ApiHandle
) -> None:
    llm = FakeLLM(
        [
            tool_call_response("scheduling__create", BOOKING_ARGS),
            text_response("Posso reservar."),
        ]
    )
    evaluator = BudgetEvaluator(
        PostgresBudgetStore(db), PostgresLLMUsageStore(db), PriceTable(), ttl_seconds=0
    )  # no budget yet: the proposal is made normally
    runtime, channel, out = runtime_with(db, api, llm, evaluator)
    await say(runtime, channel, "quero terça às 10h")
    assert "SIM" in out[-1]

    await over_budget(db)  # the tenant goes over its budget while the contact decides
    await say(runtime, channel, "sim")
    assert len(api.state.bookings) == 1  # finished: the confirmation is work under way
    assert DEFAULT_DEGRADED_REPLY not in "".join(out)
