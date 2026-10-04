"""Circuit breaking, metrics and record/replay of tool results (INV-038, INV-039)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from conversation_agent.adapters.tools.fake import FakeToolProvider
from conversation_agent.adapters.tools.metrics import InMemoryToolMetrics
from conversation_agent.adapters.tools.replay import (
    RecordingToolProvider,
    ReplayToolProvider,
    load_cassette,
)
from conversation_agent.adapters.tools.resilience import (
    CircuitBreakerToolProvider,
    MeteredToolProvider,
    is_health_failure,
)
from conversation_agent.core.definitions.binding import CapabilityBinding, ResolvedToolBinding
from conversation_agent.core.definitions.capability import CapabilityDefinition, Risk
from conversation_agent.core.definitions.tool import ToolDefinition
from conversation_agent.core.models.tooling import ToolContext, ToolError, ToolResult

from .test_mcp_provider import CONTEXT


class Args(BaseModel):
    q: str = "x"


def binding(
    risk: Risk = "read", connection: str = "erp", tool: str = "erp_call"
) -> ResolvedToolBinding:
    cap = CapabilityDefinition(
        name="a.b", description="x", input_model=Args, output_model=Args, risk=risk
    )
    definition = ToolDefinition(
        name=tool,
        description="x",
        input_model=Args,
        risk=risk,
        provider="fake",
        connection=connection,
    )
    return ResolvedToolBinding(
        capability=cap,
        tool=definition,
        binding=CapabilityBinding(capability="a.b", tool=tool, input_map={}, output_map={}),
    )


def failure(status: Any = "technical_error", code: str = "EXTERNAL_UNREACHABLE") -> ToolResult:
    return ToolResult(status=status, error=ToolError(code=code, message_safe="x", retryable=True))


OK = ToolResult(status="success", data={"ok": True})
BUSINESS = ToolResult(status="business_error", error=ToolError(code="SLOT_TAKEN", message_safe="x"))
CONFIG = ToolResult(
    status="technical_error", error=ToolError(code="INSECURE_CONNECTION", message_safe="x")
)


class Script:
    """A provider whose next answers are scripted; counts what actually reached it."""

    def __init__(self, *results: ToolResult) -> None:
        self.results = list(results)
        self.reached = 0

    async def destination_fingerprint(self, b: ResolvedToolBinding, c: ToolContext) -> str | None:
        return "fp"

    async def execute(
        self,
        b: ResolvedToolBinding,
        args: dict[str, Any],
        c: ToolContext,
        *,
        destination_fingerprint: str | None = None,
    ) -> ToolResult:
        self.reached += 1
        return self.results.pop(0) if self.results else OK


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def breaker(inner: Any, clock: Clock, **kw: Any) -> CircuitBreakerToolProvider:
    return CircuitBreakerToolProvider(
        inner, provider_name="fake", monotonic=clock, failure_threshold=3, open_seconds=30, **kw
    )


async def trip(provider: CircuitBreakerToolProvider, b: ResolvedToolBinding, n: int = 3) -> None:
    for _ in range(n):
        await provider.execute(b, {}, CONTEXT)


# --- the breaker ---


async def test_consecutive_health_failures_open_the_circuit_and_nothing_more_is_sent() -> None:
    inner, clock = Script(*[failure()] * 3), Clock()
    provider = breaker(inner, clock)
    await trip(provider, binding())
    assert provider.state("t1", "erp") == "open"
    blocked = await provider.execute(binding("write"), {}, CONTEXT)
    assert inner.reached == 3  # the fourth call never reached the provider
    assert blocked.status == "technical_error" and blocked.error is not None
    assert blocked.error.code == "CIRCUIT_OPEN" and blocked.error.retryable  # known: not sent


async def test_a_blocked_write_is_a_known_non_execution_never_unknown() -> None:
    inner, clock = Script(*[failure()] * 3), Clock()
    provider = breaker(inner, clock)
    await trip(provider, binding())
    result = await provider.execute(binding("write"), {}, CONTEXT)
    assert result.status == "technical_error"  # safe to retry with the same key, unlike `unknown`


async def test_what_the_inner_provider_answers_is_passed_through_unchanged() -> None:
    unknown = ToolResult(
        status="unknown", error=ToolError(code="EXTERNAL_TIMEOUT", message_safe="x")
    )
    provider = breaker(Script(unknown, unknown, unknown, unknown), Clock())
    results = [await provider.execute(binding("write"), {}, CONTEXT) for _ in range(3)]
    assert all(r is unknown for r in results)  # the breaker never reclassifies an ambiguous write
    assert provider.state("t1", "erp") == "open"


async def test_business_validation_and_configuration_answers_do_not_count() -> None:
    inner = Script(failure(), failure(), BUSINESS, failure(), failure(), CONFIG, CONFIG, CONFIG)
    provider = breaker(inner, Clock())
    await trip(provider, binding(), 8)
    assert provider.state("t1", "erp") == "closed"  # an answer proves it is alive; config isn't it
    assert not is_health_failure(BUSINESS) and not is_health_failure(CONFIG)
    assert is_health_failure(failure("timeout", "X")) and is_health_failure(failure("unknown", "X"))


async def test_after_the_window_one_probe_decides_and_success_closes_the_circuit() -> None:
    inner, clock = Script(*[failure()] * 3), Clock()
    provider = breaker(inner, clock)
    await trip(provider, binding())
    clock.t = 29
    assert (await provider.execute(binding(), {}, CONTEXT)).error.code == "CIRCUIT_OPEN"  # type: ignore[union-attr]
    clock.t = 31
    assert (await provider.execute(binding(), {}, CONTEXT)).status == "success"  # the probe
    assert provider.state("t1", "erp") == "closed"
    assert inner.reached == 4


async def test_a_failed_probe_reopens_for_another_full_window() -> None:
    inner, clock = Script(*[failure()] * 4), Clock()
    provider = breaker(inner, clock)
    await trip(provider, binding())
    clock.t = 31
    await provider.execute(binding(), {}, CONTEXT)  # probe fails
    assert provider.state("t1", "erp") == "open"
    clock.t = 40  # 9 s after the failed probe, not 30
    assert (await provider.execute(binding(), {}, CONTEXT)).error.code == "CIRCUIT_OPEN"  # type: ignore[union-attr]
    assert inner.reached == 4


async def test_only_the_configured_number_of_probes_are_let_through() -> None:
    inner, clock = Script(*[failure()] * 3), Clock()
    provider = breaker(inner, clock, half_open_probes=1)
    await trip(provider, binding())
    clock.t = 31
    first = provider._admit(provider._circuits[("t1", "erp")], "erp")  # type: ignore[attr-defined]
    second = provider._admit(provider._circuits[("t1", "erp")], "erp")  # type: ignore[attr-defined]
    assert (first, second) == (True, False)  # a flood does not all become probes


async def test_circuits_are_per_tenant_and_per_connection() -> None:
    inner, clock = Script(*[failure()] * 3), Clock()
    provider = breaker(inner, clock)
    await trip(provider, binding())
    other_tenant = CONTEXT.model_copy(update={"tenant_id": "t2"})
    assert (await provider.execute(binding(), {}, other_tenant)).status == "success"
    assert (await provider.execute(binding(connection="crm"), {}, CONTEXT)).status == "success"


async def test_an_exception_from_the_provider_counts_and_still_propagates() -> None:
    class Boom(Script):
        async def execute(self, *a: Any, **k: Any) -> ToolResult:
            raise RuntimeError("bug")

    provider = breaker(Boom(), Clock())
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await provider.execute(binding(), {}, CONTEXT)
    assert provider.state("t1", "erp") == "open"


def test_a_circuit_needs_sane_settings() -> None:
    with pytest.raises(ValueError):
        CircuitBreakerToolProvider(Script(), provider_name="x", failure_threshold=0)


async def test_the_destination_fingerprint_is_delegated() -> None:
    provider = breaker(Script(), Clock())
    assert await provider.destination_fingerprint(binding(), CONTEXT) == "fp"


# --- metrics ---


async def test_every_call_is_counted_by_name_and_canonical_code_never_by_content() -> None:
    metrics = InMemoryToolMetrics()
    inner, clock = Script(OK, failure(), BUSINESS, failure(), failure(), failure()), Clock()
    provider = MeteredToolProvider(
        breaker(inner, clock, metrics=metrics), metrics, provider_name="fake", monotonic=clock
    )
    for _ in range(7):
        clock.t += 0.5
        await provider.execute(binding(), {"q": "secret-ish"}, CONTEXT)
    assert metrics.calls[("fake", "erp_call", "success", None)] == 1
    assert metrics.calls[("fake", "erp_call", "business_error", "SLOT_TAKEN")] == 1
    assert metrics.calls[("fake", "erp_call", "technical_error", "CIRCUIT_OPEN")] >= 1
    assert metrics.latency[("fake", "erp_call")].count == 7
    assert metrics.circuits == [("fake", "erp", "open")]
    assert "secret-ish" not in repr(metrics)


async def test_a_failing_metrics_backend_never_changes_a_tool_call() -> None:
    class Broken:
        def record_call(self, **kw: Any) -> None:
            raise RuntimeError("down")

        def record_circuit(self, **kw: Any) -> None:
            raise RuntimeError("down")

    provider = MeteredToolProvider(Script(OK), Broken(), provider_name="fake")
    assert (await provider.execute(binding(), {}, CONTEXT)).status == "success"
    tripping = breaker(Script(*[failure()] * 3), Clock(), metrics=Broken())
    await trip(tripping, binding())
    assert tripping.state("t1", "erp") == "open"


# --- record / replay ---


async def test_a_recording_replays_without_any_transport(tmp_path: Path) -> None:
    live = FakeToolProvider(
        {"erp_call": lambda a: ToolResult(status="success", data={"echo": a["q"]})}
    )
    recorder = RecordingToolProvider(live)
    first = await recorder.execute(binding(), {"q": "a"}, CONTEXT)
    await recorder.execute(binding(), {"q": "b"}, CONTEXT)
    recorder.save(tmp_path / "cassette.json")

    replay = ReplayToolProvider(load_cassette(tmp_path / "cassette.json"))
    again = await replay.execute(binding(), {"q": "a"}, CONTEXT)
    assert again.data == first.data == {"echo": "a"}
    assert (await replay.execute(binding(), {"q": "b"}, CONTEXT)).data == {"echo": "b"}


async def test_an_unrecorded_call_is_an_explicit_miss_never_an_invented_answer() -> None:
    replay = ReplayToolProvider({})
    miss = await replay.execute(binding(), {"q": "never seen"}, CONTEXT)
    assert miss.status == "technical_error" and miss.error is not None
    assert miss.error.code == "REPLAY_MISS" and not miss.error.retryable
    assert await replay.destination_fingerprint(binding(), CONTEXT) is None


async def test_a_call_is_served_in_recorded_order_and_then_runs_out() -> None:
    live = Script(failure(), OK)
    recorder = RecordingToolProvider(live)
    await recorder.execute(binding(), {"q": "a"}, CONTEXT)
    await recorder.execute(binding(), {"q": "a"}, CONTEXT)
    replay = ReplayToolProvider(dict(recorder.cassette))
    statuses = [(await replay.execute(binding(), {"q": "a"}, CONTEXT)).status for _ in range(3)]
    assert statuses == ["technical_error", "success", "technical_error"]  # third = REPLAY_MISS
    assert (await replay.execute(binding(), {"q": "a"}, CONTEXT)).error.code == "REPLAY_MISS"  # type: ignore[union-attr]


async def test_recording_redacts_personal_data_and_drops_operational_metadata() -> None:
    live = FakeToolProvider(
        {
            "erp_call": ToolResult(
                status="success",
                data={"contact": "ana@example.com", "cpf": "123.456.789-09", "n": 3},
                provider_metadata={"http_status": 200},
            )
        }
    )
    recorder = RecordingToolProvider(live)
    await recorder.execute(binding(), {"q": "a"}, CONTEXT)
    (kept,) = next(iter(recorder.cassette.values()))
    assert kept.data == {"contact": "[REDACTED_EMAIL]", "cpf": "[REDACTED_CPF]", "n": 3}
    assert kept.provider_metadata == {}
    raw = RecordingToolProvider(live, redact_data=False)
    await raw.execute(binding(), {"q": "a"}, CONTEXT)
    assert next(iter(raw.cassette.values()))[0].data["contact"] == "ana@example.com"  # type: ignore[index]
