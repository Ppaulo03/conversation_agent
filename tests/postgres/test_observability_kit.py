"""The wiring helper: one call turns on JSON logs, span lines, the price list and the metrics, and
a real turn shows up in all of them."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import httpx
import pytest

from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.llm.usage_memory import InMemoryLLMUsageStore
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.app.observability import setup_observability
from conversation_agent.core.models.llm import LLMUsage
from conversation_agent.core.tracing import configure_tracer
from postgres.world import World, event


@pytest.fixture
def world(db: PostgresDatabase, clock: FixedClock) -> World:
    return World(db, clock)


def teardown() -> None:
    configure_tracer(None)
    logger = logging.getLogger("conversation_agent")
    for handler in list(logger.handlers):
        if getattr(handler, "_conversation_agent_json", False):
            logger.removeHandler(handler)
    logger.propagate = True


async def test_a_turn_through_the_kit_leaves_logs_spans_usage_and_metrics(
    world: World, tmp_path: Path
) -> None:
    prices = tmp_path / "prices.yaml"
    prices.write_text(
        "models:\n  fake/m:\n    - {effective_from: 2020-01-01, input_per_mtok: 10, output_per_mtok: 10}\n",
        encoding="utf-8",
    )
    out = io.StringIO()
    try:
        obs = setup_observability(environ={"LLM_PRICES_FILE": str(prices)}, stream=out)
        ledger = InMemoryLLMUsageStore()
        answer = text_response("Olá!").model_copy(
            update={
                "provider": "fake",
                "model": "m",
                "usage": LLMUsage(input_tokens=1000, output_tokens=500),
            }
        )
        llm = obs.wrap_llm(FakeLLM([answer]), ledger)

        await world.inbox.insert_if_absent(event("e-1", "oi", clock=world.clock))
        await world.coordinator("w", llm, metrics=obs.runtime_metrics).run_once()

        (record,) = ledger.records
        assert record.tenant_id == "tenant-1" and record.purpose == "agent" and record.turn_id
        assert record.trace_id and record.agent_version  # attributed, correlated
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        spans = [x for x in lines if x["event"] == "span"]
        assert {"turn", "llm.call"} <= {x["span"] for x in spans}
        assert {x["trace_id"] for x in spans} == {record.trace_id}

        transport = httpx.ASGITransport(app=obs.ops_app(world.db))
        async with httpx.AsyncClient(transport=transport, base_url="http://ops") as client:
            text = (await client.get("/metrics")).text
        assert (
            'conversation_agent_llm_calls_total{provider="fake",model="m",purpose="agent",outcome="ok"'
            in text
        )
        assert "conversation_agent_llm_cost_usd_total" in text and "0.01500000" in text
        assert "conversation_agent_turns_total" in text and 'outcome="completed"' in text
    finally:
        teardown()


def test_the_environment_controls_logging_and_tracing_and_a_missing_price_list_is_empty() -> None:
    try:
        obs = setup_observability(
            environ={"LOG_LEVEL": "debug", "TRACE": "off", "LLM_PRICES_FILE": "/no/such/file.yaml"},
            stream=io.StringIO(),
        )
        assert obs.prices.models == {}  # every model is then UNPRICED, visibly
        assert logging.getLogger("conversation_agent").level == logging.DEBUG
    finally:
        teardown()
