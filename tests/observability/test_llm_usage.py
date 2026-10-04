"""LLM observability (Phase 11): normalised usage, prices as data, the metered provider, the
ledger contract (in memory and on PostgreSQL), and the Prometheus exposition of LLM calls."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from conversation_agent.adapters.llm.anthropic import AnthropicLLM
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.llm.metered import MeteredLLMProvider
from conversation_agent.adapters.llm.metrics import InMemoryLLMMetrics
from conversation_agent.adapters.llm.openai_compat import OpenAICompatLLM
from conversation_agent.adapters.llm.usage_memory import InMemoryLLMUsageStore
from conversation_agent.adapters.observability.prices import load_prices
from conversation_agent.adapters.observability.prometheus import (
    PREFIX,
    available_metrics,
    render,
)
from conversation_agent.adapters.postgres.health import HealthSnapshot
from conversation_agent.core.errors import DefinitionError, LLMProviderError
from conversation_agent.core.llm_prices import ModelPrice, PriceTable, cost_of
from conversation_agent.core.models.llm import LLMRequest, LLMResponse, LLMUsage
from conversation_agent.core.models.llm_usage import UNATTRIBUTED_TENANT, LLMCallRecord, UsageQuery
from conversation_agent.core.observability import bind

ROOT = Path(__file__).resolve().parents[2]
JAN = datetime(2026, 1, 15, 10, tzinfo=UTC)


def table() -> PriceTable:
    return PriceTable(
        models={
            "anthropic/claude-x": (
                ModelPrice(
                    effective_from=date(2026, 1, 1),
                    input_per_mtok=3,
                    output_per_mtok=15,
                    cache_read_per_mtok=0.3,
                    cache_write_per_mtok=3.75,
                ),
                ModelPrice(effective_from=date(2026, 2, 1), input_per_mtok=2, output_per_mtok=10),
            ),
            "small": (
                ModelPrice(effective_from=date(2026, 1, 1), input_per_mtok=1, output_per_mtok=2),
            ),
        }
    )


# --- prices are data ---


def test_cost_is_tokens_times_the_price_in_force_on_the_day_of_the_call() -> None:
    prices = table()
    jan = cost_of(prices, provider="anthropic", model="claude-x", at=JAN, input_tokens=1_000_000,
                  output_tokens=100_000, cache_read_tokens=2_000_000, cache_write_tokens=1_000_000)  # fmt: skip
    assert jan == pytest.approx(3 + 1.5 + 0.6 + 3.75)
    feb = cost_of(prices, provider="anthropic", model="claude-x", at=JAN + timedelta(days=30),
                  input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=1_000_000)  # fmt: skip
    assert feb == pytest.approx(
        2 + 1 + 2
    )  # the new price; the unset cache price is the input price


def test_an_unlisted_model_or_a_date_before_any_price_is_unpriced_never_free() -> None:
    prices = table()
    assert (
        cost_of(prices, provider="x", model="nope", at=JAN, input_tokens=5, output_tokens=5) is None
    )
    early = JAN - timedelta(days=60)
    assert (
        cost_of(
            prices,
            provider="anthropic",
            model="claude-x",
            at=early,
            input_tokens=1,
            output_tokens=1,
        )
        is None
    )
    assert (
        cost_of(prices, provider=None, model=None, at=JAN, input_tokens=1, output_tokens=1) is None
    )
    # the bare model name is a fallback for any provider; the qualified key wins when both exist
    assert (
        cost_of(
            prices,
            provider="whoever",
            model="small",
            at=JAN,
            input_tokens=1_000_000,
            output_tokens=0,
        )
        == 1.0
    )
    both = PriceTable(
        models={
            **table().models,
            "p/small": (
                ModelPrice(effective_from=date(2026, 1, 1), input_per_mtok=9, output_per_mtok=9),
            ),
        }
    )
    assert (
        cost_of(both, provider="p", model="small", at=JAN, input_tokens=1_000_000, output_tokens=0)
        == 9.0
    )


def test_two_prices_for_one_day_are_ambiguous_and_refused() -> None:
    price = ModelPrice(effective_from=date(2026, 1, 1), input_per_mtok=1, output_per_mtok=1)
    with pytest.raises(ValidationError, match="same effective_from"):
        PriceTable(models={"m": (price, price)})


def test_the_price_file_loads_and_the_shipped_one_prices_nothing_it_does_not_know(
    tmp_path: Path,
) -> None:
    shipped = load_prices(ROOT / "ops" / "llm_prices.yaml")
    assert shipped.models == {} and shipped.currency == "USD"  # no invented prices
    good = tmp_path / "p.yaml"
    good.write_text(
        "models:\n  m:\n    - {effective_from: 2026-01-01, input_per_mtok: 1, output_per_mtok: 2}\n",
        encoding="utf-8",
    )
    assert load_prices(good).price_for(None, "m", JAN) is not None
    empty = tmp_path / "e.yaml"
    empty.write_text("", encoding="utf-8")
    assert load_prices(empty).models == {}
    bad = tmp_path / "b.yaml"
    bad.write_text("models: {m: [{effective_from: x}]}", encoding="utf-8")
    with pytest.raises(DefinitionError):
        load_prices(bad)
    with pytest.raises(DefinitionError):
        load_prices(tmp_path / "missing.yaml")


# --- usage is normalised by the adapters ---


def test_openai_style_usage_splits_the_cached_tokens_out_of_the_input() -> None:
    payload = {
        "model": "gpt-x",
        "choices": [{"message": {"content": "oi"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 300,
            "prompt_tokens_details": {"cached_tokens": 800},
            "completion_tokens_details": {"reasoning_tokens": 120},
        },
    }
    response = OpenAICompatLLM._to_response(payload, None)
    assert response.usage == LLMUsage(
        input_tokens=200, output_tokens=300, cache_read_tokens=800, reasoning_tokens=120
    )
    assert (response.provider, response.model) == ("openai_compat", "gpt-x")
    assert response.usage.total_tokens == 1300
    bare = OpenAICompatLLM._to_response(
        {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]}, None
    )
    assert bare.usage == LLMUsage() and bare.model is None


def test_anthropic_usage_keeps_its_cache_counters_and_names_the_model() -> None:
    raw = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="oi")],
        stop_reason="end_turn",
        model="claude-x",
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=40,
            cache_read_input_tokens=900,
            cache_creation_input_tokens=50,
        ),
    )
    response = AnthropicLLM._to_response(raw, None)
    assert response.usage == LLMUsage(
        input_tokens=100, output_tokens=40, cache_read_tokens=900, cache_write_tokens=50
    )
    assert (response.provider, response.model) == ("anthropic", "claude-x")
    older = SimpleNamespace(
        content=[], stop_reason="end_turn", usage=SimpleNamespace(input_tokens=1, output_tokens=2)
    )
    assert AnthropicLLM._to_response(older, None).usage.cache_read_tokens == 0


# --- the metered provider ---


def request() -> LLMRequest:
    return LLMRequest(system="s", messages=(), request_id="req-1")


def metered(
    inner: Any, **kw: Any
) -> tuple[MeteredLLMProvider, InMemoryLLMUsageStore, InMemoryLLMMetrics]:
    store, metrics = InMemoryLLMUsageStore(), InMemoryLLMMetrics()
    provider = MeteredLLMProvider(
        inner,
        store,
        metrics=metrics,
        prices=table(),
        provider_name="fake",
        model_name="small",
        now=lambda: JAN,
        **kw,
    )
    return provider, store, metrics


def answered(model: str = "claude-x") -> LLMResponse:
    return text_response("ok").model_copy(
        update={
            "provider": "anthropic",
            "model": model,
            "usage": LLMUsage(
                input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=500_000
            ),
        }
    )


async def test_a_call_is_recorded_with_who_why_what_it_used_and_how_long_it_took() -> None:
    ticks = iter([0.0, 2.5])
    provider, store, metrics = metered(FakeLLM([answered()]), monotonic=lambda: next(ticks))
    with bind(tenant_id="t1", agent_id="support", agent_version="1.2.0", turn_id="turn-1",
              conversation_ref="abc", purpose="confirmation_decision", trace_id="tr-1"):  # fmt: skip
        await provider.complete(request())
    (record,) = store.records
    assert (record.tenant_id, record.agent_id, record.agent_version) == ("t1", "support", "1.2.0")
    assert (record.purpose, record.turn_id, record.conversation_ref, record.trace_id) == (
        "confirmation_decision", "turn-1", "abc", "tr-1"
    )  # fmt: skip
    assert (record.provider, record.model, record.request_id) == ("anthropic", "claude-x", "req-1")
    assert (record.input_tokens, record.output_tokens, record.cache_read_tokens) == (
        1_000_000,
        100_000,
        500_000,
    )
    assert record.latency_ms == 2500 and record.outcome == "ok" and record.stop_reason == "end_turn"
    # and the same call is in the counters, priced
    key = ("anthropic", "claude-x", "confirmation_decision")
    assert metrics.cost_usd[key] == pytest.approx(3 + 1.5 + 0.15)
    assert metrics.tokens[(*key, "cache_read")] == 500_000 and metrics.latency[key].count == 1


async def test_a_failed_call_is_recorded_by_exception_type_and_still_raises() -> None:
    provider, store, metrics = metered(FakeLLM([LLMProviderError("boom for ana@example.com")]))
    with bind(tenant_id="t1"), pytest.raises(LLMProviderError):
        await provider.complete(request())
    (record,) = store.records
    assert (record.outcome, record.error_code, record.input_tokens) == (
        "error",
        "LLMProviderError",
        0,
    )
    assert "ana@example.com" not in record.model_dump_json()  # never the message
    assert metrics.calls[("fake", "small", "agent", "error", "LLMProviderError")] == 1


async def test_a_call_outside_any_context_is_still_on_the_books_as_unattributed() -> None:
    provider, store, _ = metered(FakeLLM([text_response("ok")]))
    await provider.complete(request())
    (record,) = store.records
    assert record.tenant_id == UNATTRIBUTED_TENANT and record.purpose == "agent"
    assert (record.provider, record.model) == ("fake", "small")  # the configured names fill in


async def test_a_model_without_a_price_is_counted_as_unpriced_not_free() -> None:
    provider, _, metrics = metered(FakeLLM([answered("mystery-model")]))
    with bind(tenant_id="t1"):
        await provider.complete(request())
    assert metrics.unpriced[("anthropic", "mystery-model")] == 1 and metrics.cost_usd == {}


async def test_a_failing_ledger_never_fails_the_call_and_is_counted_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Broken:
        async def record(self, call: Any) -> None:
            raise ConnectionError("db down")

    metrics = InMemoryLLMMetrics()
    provider = MeteredLLMProvider(FakeLLM([text_response("ok")]), Broken(), metrics=metrics)  # type: ignore[arg-type]
    with caplog.at_level(logging.ERROR), bind(tenant_id="t1"):
        response = await provider.complete(request())
    assert response.text == "ok"  # the customer still gets the answer
    assert metrics.usage_write_failures == 1
    assert any(r.getMessage() == "llm_usage.record_failed" for r in caplog.records)


async def test_a_failing_metrics_backend_never_changes_a_call() -> None:
    class Broken:
        def record_llm_call(self, **kw: Any) -> None:
            raise RuntimeError("down")

        def record_usage_write_failure(self) -> None:
            raise RuntimeError("down")

    provider = MeteredLLMProvider(
        FakeLLM([text_response("ok")]), InMemoryLLMUsageStore(), metrics=Broken()
    )  # type: ignore[arg-type]
    assert (await provider.complete(request())).text == "ok"


# --- the ledger contract ---


def call(**kw: Any) -> LLMCallRecord:
    base: dict[str, Any] = {
        "tenant_id": "t1", "started_at": JAN, "purpose": "agent", "agent_id": "support",
        "agent_version": "1.0.0", "provider": "anthropic", "model": "claude-x",
        "input_tokens": 1_000_000, "output_tokens": 100_000, "latency_ms": 1000.0,
    }  # fmt: skip
    return LLMCallRecord.model_validate({**base, **kw})


async def seed(store: Any) -> None:
    for record in (
        call(latency_ms=1000),
        call(
            latency_ms=3000,
            outcome="error",
            error_code="LLMProviderError",
            input_tokens=0,
            output_tokens=0,
        ),
        call(
            purpose="confirmation_decision",
            agent_version="1.1.0",
            input_tokens=500_000,
            output_tokens=0,
            latency_ms=200,
        ),
        call(
            started_at=JAN + timedelta(days=30), input_tokens=1_000_000, output_tokens=0
        ),  # February price
        call(model="mystery", input_tokens=10, output_tokens=10),
        call(tenant_id="other", input_tokens=9_000_000),
    ):
        await store.record(record)


def query(**kw: Any) -> UsageQuery:
    base: dict[str, Any] = {
        "tenant_id": "t1",
        "since": JAN - timedelta(days=1),
        "until": JAN + timedelta(days=60),
    }
    return UsageQuery.model_validate({**base, **kw})


async def contract(store: Any) -> None:
    await seed(store)
    prices = table()
    (total,) = await store.report(query(), prices)
    assert total.calls == 5 and total.errors == 1 and total.unpriced_calls == 1
    assert total.input_tokens == 2_500_010
    # Jan: 1.5M in x $3 + 100k out x $15 ; Feb: 1M in x $2 ; the unpriced call adds no cost
    assert total.cost_usd == pytest.approx(4.5 + 1.5 + 2.0)
    assert total.latency_p95_ms == pytest.approx(2800.0)  # worst of the merged parts

    by_version = {
        r.keys["agent_version"]: r
        for r in await store.report(query(group_by=("agent_version",)), prices)
    }
    assert set(by_version) == {"1.0.0", "1.1.0"} and by_version["1.1.0"].calls == 1
    assert by_version["1.1.0"].cost_usd == pytest.approx(
        1.5
    )  # a canary can be compared per version

    by_purpose = {
        r.keys["purpose"]: r.calls for r in await store.report(query(group_by=("purpose",)), prices)
    }
    assert by_purpose == {"agent": 4, "confirmation_decision": 1}

    by_model = {r.model: r for r in await store.report(query(group_by=("model",)), prices)}
    assert by_model["mystery"].unpriced_calls == 1 and by_model["mystery"].cost_usd == 0.0
    assert by_model["claude-x"].unpriced_calls == 0

    days = {r.keys["day"]: r.cost_usd for r in await store.report(query(group_by=("day",)), prices)}
    assert set(days) == {"2026-01-15", "2026-02-14"} and days["2026-02-14"] == pytest.approx(2.0)

    assert (await store.report(query(tenant_id="nobody"), prices)) == []
    assert (await store.report(query(agent_id="other-agent"), prices)) == []
    narrow = await store.report(query(since=JAN, until=JAN + timedelta(days=1)), prices)
    assert narrow[0].calls == 4  # the February call is outside the window
    multi = await store.report(query(group_by=("agent_version", "purpose")), prices)
    assert {(r.keys["agent_version"], r.keys["purpose"]) for r in multi} == {
        ("1.0.0", "agent"), ("1.1.0", "confirmation_decision")
    }  # fmt: skip


async def test_the_in_memory_ledger_follows_the_contract() -> None:
    await contract(InMemoryLLMUsageStore())


def test_a_record_cannot_carry_negative_numbers_or_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        call(input_tokens=-1)
    with pytest.raises(ValidationError):
        LLMCallRecord.model_validate(
            {**call().model_dump(), "prompt": "secret"}
        )  # no content field


# --- the exposition ---


def test_llm_counters_and_a_valid_histogram_are_exposed() -> None:
    llm = InMemoryLLMMetrics()
    for seconds in (0.1, 0.6, 3.0, 100.0):
        llm.record_llm_call(provider="anthropic", model="claude-x", purpose="agent", outcome="ok",
                            error_code=None, input_tokens=10, output_tokens=5, cache_read_tokens=0,
                            cache_write_tokens=0, duration_seconds=seconds, cost_usd=0.01)  # fmt: skip
    llm.record_llm_call(provider="p", model="m", purpose="agent", outcome="error", error_code="X",
                        input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0,
                        duration_seconds=1.0, cost_usd=None)  # fmt: skip
    text = render(HealthSnapshot(), None, llm)
    base = f'{PREFIX}llm_call_duration_seconds_bucket{{provider="anthropic",model="claude-x",purpose="agent",'
    counts = [int(line.rsplit(" ", 1)[1]) for line in text.splitlines() if line.startswith(base)]
    assert (
        counts == sorted(counts) and counts[0] == 1 and counts[-1] == 4
    )  # cumulative, +Inf = count
    assert (
        f'{PREFIX}llm_call_duration_seconds_count{{provider="anthropic",model="claude-x",purpose="agent"}} 4'
        in text
    )
    assert f'{PREFIX}llm_unpriced_calls_total{{provider="p",model="m"}} 1' in text
    assert (
        f'{PREFIX}llm_cost_usd_total{{provider="anthropic",model="claude-x",purpose="agent"}} 0.04000000'
        in text
    )
    assert (
        f'{PREFIX}llm_calls_total{{provider="p",model="m",purpose="agent",outcome="error",error_code="X"}} 1'
        in text
    )
    names = {
        line.split("{")[0].split(" ")[0] for line in text.splitlines() if not line.startswith("#")
    }
    assert names <= available_metrics()
