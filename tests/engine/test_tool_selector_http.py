"""The `http` tool selector: an external ranking server, reached like any other outbound system."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from conftest import ApiHandle
from conversation_agent.adapters.connections.static import StaticConnectionResolver
from conversation_agent.adapters.llm.fake import FakeLLM, text_response
from conversation_agent.adapters.secrets.providers import InMemorySecretProvider
from conversation_agent.adapters.tool_selection.factory import tool_selector_for
from conversation_agent.adapters.tool_selection.http import HttpToolSelector
from conversation_agent.adapters.tools.guard import GuardedTransport
from conversation_agent.app import tool_selection_eval as ev
from conversation_agent.core.definitions.agent import AgentDefinition
from conversation_agent.core.definitions.tool_selection import (
    HttpSelectorSettings,
    ToolSelectionSettings,
)
from conversation_agent.core.errors import ToolSelectorCircuitOpenError, ToolSelectorError
from conversation_agent.core.models.connections import AuthSpec, ResolvedConnection
from conversation_agent.core.models.conversation import ConversationState
from conversation_agent.core.models.llm import LLMToolDefinition
from support.builders import IDENTITY, new_clock, new_journal
from tool_selector_api.main import create_app
from vertical_slice.definitions import build_agent
from vertical_slice.wiring import build_engine

DATA = Path(__file__).resolve().parents[2] / "examples" / "tool-selection-eval"
TENANT = IDENTITY.tenant_id
AVAIL, CREATE = "scheduling__availability", "scheduling__create"


def conn(**over: Any) -> ResolvedConnection:
    base: dict[str, Any] = {
        "connection_id": "sel",
        "base_url": "http://127.0.0.1:8002",
        "allow_private_networks": True,
        "tls_required": False,
    }
    return ResolvedConnection(**{**base, **over})


def guard_for(
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    *,
    connection: ResolvedConnection | None = None,
    token: str | None = None,
    app_token: str | None = None,
) -> GuardedTransport:
    transport: httpx.AsyncBaseTransport = (
        httpx.MockTransport(handler)
        if handler is not None
        else httpx.ASGITransport(app=create_app(app_token))
    )
    secrets = InMemorySecretProvider({(TENANT, "tok"): token}) if token else None
    return GuardedTransport(
        StaticConnectionResolver({"sel": connection or conn()}),
        secrets,
        httpx.AsyncClient(transport=transport, follow_redirects=False),
    )


def selector(guard: GuardedTransport, **over: Any) -> HttpToolSelector:
    return HttpToolSelector(guard, HttpSelectorSettings(connection="sel", **over), top_n=8)


def tools(*names: str) -> tuple[LLMToolDefinition, ...]:
    return tuple(
        LLMToolDefinition(name=n, description=f"{n} descricao horarios", input_schema={})
        for n in names
    )


def answer(body: Any, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, json=body)


async def test_the_reference_server_ranks_through_the_contract() -> None:
    catalog = ev.load_catalog(DATA / "catalog.json")
    ranked = await selector(guard_for()).rank(
        "quais horários livres amanhã?", catalog, tenant_id=TENANT
    )
    scores = {s.name: s.score for s in ranked}
    assert scores["agenda_list_availability"] > scores["agenda_cancel_booking"]
    assert set(scores) == {t.name for t in catalog}


async def test_the_request_carries_only_the_message_tool_names_and_the_credential() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"], seen["body"] = request.headers, json.loads(request.content)
        return httpx.Response(200, json={"version": 1, "scores": []})

    connection = conn(auth=AuthSpec(secret_ref="tok"))
    await selector(guard_for(handler, connection=connection, token="s3cret")).rank(
        "oi", tools(AVAIL), tenant_id=TENANT
    )
    assert seen["headers"]["authorization"] == "Bearer s3cret"
    assert seen["headers"]["x-tenant-id"] == TENANT
    assert set(seen["body"]) == {"version", "message", "tools", "top_n"}
    assert set(seen["body"]["tools"][0]) == {"name", "description"}  # no schema, no history


async def test_a_wrong_token_is_a_failed_call() -> None:
    connection = conn(auth=AuthSpec(secret_ref="tok"))
    guard = guard_for(connection=connection, token="wrong", app_token="right")
    with pytest.raises(ToolSelectorError, match="HTTP 401"):
        await selector(guard).rank("oi", tools(AVAIL), tenant_id=TENANT)


async def test_names_the_server_invents_are_ignored_and_missing_ones_score_zero() -> None:
    body = {
        "version": 1,
        "scores": [{"name": "ghost", "score": 1.0}, {"name": AVAIL, "score": 0.9}],
    }
    ranked = await selector(guard_for(answer(body))).rank(
        "x", tools(AVAIL, CREATE), tenant_id=TENANT
    )
    assert {s.name: s.score for s in ranked} == {AVAIL: 0.9, CREATE: 0.0}


@pytest.mark.parametrize(
    ("handler", "match"),
    [
        (answer({}, 500), "HTTP 500"),
        (answer({"version": 2, "scores": []}), "contract v2"),
        (answer({"version": 1, "scores": [{"name": AVAIL, "score": 7}]}), "not valid"),
        (answer({"version": 1}), "not valid"),
        (lambda r: httpx.Response(200, content=b"not json"), "not valid"),
        (lambda r: httpx.Response(200, content=b"x" * 2_000_000), "too large"),
        (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), "ReadTimeout"),
        (lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")), "ConnectError"),
    ],
)
async def test_any_bad_answer_is_a_failed_call(handler: Any, match: str) -> None:
    with pytest.raises(ToolSelectorError, match=match):
        await selector(guard_for(handler)).rank("secret text", tools(AVAIL), tenant_id=TENANT)


async def test_errors_never_carry_the_message_or_the_secret() -> None:
    connection = conn(auth=AuthSpec(secret_ref="tok"))
    with pytest.raises(ToolSelectorError) as info:
        await selector(guard_for(answer({}, 500), connection=connection, token="s3cret")).rank(
            "meu cpf 123", tools(AVAIL), tenant_id=TENANT
        )
    assert "123" not in str(info.value) and "s3cret" not in str(info.value)


async def test_a_private_address_is_refused_unless_the_connection_allows_it() -> None:
    guard = guard_for(connection=conn(allow_private_networks=False))
    with pytest.raises(ToolSelectorError, match="DESTINATION_NOT_ALLOWED"):
        await selector(guard).rank("oi", tools(AVAIL), tenant_id=TENANT)


async def test_plain_http_is_refused_when_tls_is_required() -> None:
    with pytest.raises(ToolSelectorError, match="INSECURE_CONNECTION"):
        await selector(guard_for(connection=conn(tls_required=True))).rank(
            "oi", tools(AVAIL), tenant_id=TENANT
        )


async def test_an_unknown_connection_or_a_missing_secret_is_a_failed_call() -> None:
    other = HttpToolSelector(guard_for(), HttpSelectorSettings(connection="nope"), top_n=8)
    with pytest.raises(ToolSelectorError, match="CONNECTION_NOT_CONFIGURED"):
        await other.rank("oi", tools(AVAIL), tenant_id=TENANT)
    guard = guard_for(connection=conn(auth=AuthSpec(secret_ref="tok")))  # no secret provider
    with pytest.raises(ToolSelectorError, match="SECRET_NOT_AVAILABLE"):
        await selector(guard).rank("oi", tools(AVAIL), tenant_id=TENANT)


async def test_a_redirect_is_not_followed() -> None:
    redirect = lambda r: httpx.Response(302, headers={"Location": "http://169.254.169.254/"})  # noqa: E731
    with pytest.raises(ToolSelectorError, match="HTTP 302"):
        await selector(guard_for(redirect)).rank("oi", tools(AVAIL), tenant_id=TENANT)


def test_settings_need_a_coherent_http_block() -> None:
    base: dict[str, Any] = {"enabled": True, "max_tools_per_call": 1}
    with pytest.raises(ValidationError, match="needs an http block"):
        ToolSelectionSettings(provider="http", **base)
    http = HttpSelectorSettings(connection="sel")
    with pytest.raises(ValidationError, match="needs provider 'http'"):
        ToolSelectionSettings(
            provider="rules", rules=[{"capability": "a.b", "keywords": ["x"]}], http=http, **base
        )
    for path in ("tool-selection", "/a?x=1", "/a/../b"):
        with pytest.raises(ValidationError):
            HttpSelectorSettings(connection="sel", path=path)
    assert ToolSelectionSettings(provider="http", http=http, **base).http == http


def test_the_http_provider_needs_a_guard() -> None:
    agent = build_agent()
    fields = {n: getattr(agent, n) for n in AgentDefinition.model_fields}
    sel = ToolSelectionSettings(
        enabled=True,
        provider="http",
        max_tools_per_call=1,
        http=HttpSelectorSettings(connection="sel"),
    )
    with_http = AgentDefinition(**{**fields, "tool_selection": sel})
    with pytest.raises(ValueError, match="GuardedTransport"):
        tool_selector_for(with_http)
    assert isinstance(tool_selector_for(with_http, guard=guard_for()), HttpToolSelector)


async def test_the_engine_filters_through_the_server_and_falls_back_when_it_fails(
    api: ApiHandle,
) -> None:
    agent = build_agent()
    fields = {n: getattr(agent, n) for n in AgentDefinition.model_fields}
    sel = ToolSelectionSettings(
        enabled=True,
        provider="http",
        max_tools_per_call=1,
        min_score=0.4,
        http=HttpSelectorSettings(connection="sel"),
    )
    with_http = AgentDefinition(**{**fields, "tool_selection": sel})

    async def run(guard: GuardedTransport, turn: str) -> set[str]:
        llm = FakeLLM([text_response("ok")])
        engine, _, _ = build_engine(
            llm,
            api_base_url=api.base_url,
            journal=new_journal(),
            clock=new_clock(),
            agent=with_http,
            tool_selection_guard=guard,
        )
        await engine.process_turn(IDENTITY, ConversationState(), "disponibilidade", turn)
        return {t.name for t in llm.requests[0].tools}

    body = {"version": 1, "scores": [{"name": AVAIL, "score": 0.9}, {"name": CREATE, "score": 0.1}]}
    assert await run(guard_for(answer(body)), "t1") == {AVAIL}
    assert await run(guard_for(answer({}, 500)), "t2") == {AVAIL, CREATE}  # full list


async def test_the_reference_server_is_measurable_with_the_evaluation_runner() -> None:
    catalog = ev.load_catalog(DATA / "catalog.json")
    examples = ev.load_dataset(DATA / "heldout.jsonl", catalog)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()))
    chosen = ev.http_selector("http://127.0.0.1:8002", "/tool-selection", None, 8, client)
    report = await ev.evaluate(chosen, catalog, examples, ToolSelectionSettings())
    assert report.examples == len(examples) and 0 <= report.hit_rate <= 1
    assert report.fallback_rate < 1  # the server answered: it is not the full list every time


# --- circuit breaker ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class Circuits:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []

    def record_circuit(self, *, provider: str, scope: str, state: str) -> None:
        self.events.append((provider, scope, state))


def tripping(
    handler: Callable[[httpx.Request], httpx.Response],
    clock: Clock,
    metrics: Circuits | None = None,
    **circuit: Any,
) -> tuple[HttpToolSelector, list[int]]:
    calls: list[int] = []

    def counted(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return handler(request)

    settings = HttpSelectorSettings(
        connection="sel", circuit={"failure_threshold": 3, "open_seconds": 30, **circuit}
    )
    chosen = HttpToolSelector(
        guard_for(counted), settings, top_n=8, metrics=metrics, monotonic=clock
    )
    return chosen, calls


async def fail_n(chosen: HttpToolSelector, n: int) -> None:
    for _ in range(n):
        with pytest.raises(ToolSelectorError):
            await chosen.rank("oi", tools(AVAIL), tenant_id=TENANT)


async def test_the_circuit_opens_after_consecutive_health_failures_and_stops_calling() -> None:
    clock, events = Clock(), Circuits()
    chosen, calls = tripping(answer({}, 503), clock, events)
    await fail_n(chosen, 3)
    assert chosen.circuit_state(TENANT) == "open" and len(calls) == 3
    with pytest.raises(ToolSelectorCircuitOpenError):
        await chosen.rank("oi", tools(AVAIL), tenant_id=TENANT)
    assert len(calls) == 3  # not called while open
    assert events.events == [("tool_selector", "sel", "open")]


async def test_a_probe_after_the_window_closes_or_reopens_the_circuit() -> None:
    clock, events = Clock(), Circuits()
    healthy = {"version": 1, "scores": [{"name": AVAIL, "score": 0.9}]}
    state = {"body": answer({}, 500)}
    chosen, calls = tripping(lambda r: state["body"](r), clock, events)
    await fail_n(chosen, 3)
    clock.t = 31  # window over: one probe, and it fails -> open again
    await fail_n(chosen, 1)
    assert chosen.circuit_state(TENANT) == "open" and len(calls) == 4
    clock.t = 62
    state["body"] = answer(healthy)  # the server recovered: the probe closes the circuit
    assert await chosen.rank("oi", tools(AVAIL), tenant_id=TENANT)
    assert chosen.circuit_state(TENANT) == "closed"
    assert [e[2] for e in events.events] == ["open", "half_open", "open", "half_open", "closed"]


async def test_only_health_failures_count_against_the_circuit() -> None:
    clock = Clock()
    for status in (401, 403, 404):  # the server answers: our configuration, not its health
        chosen, _ = tripping(answer({}, status), clock)
        await fail_n(chosen, 5)
        assert chosen.circuit_state(TENANT) == "closed"
    refused, _ = tripping(answer({}), Clock())
    refused._guard = guard_for(connection=conn(tls_required=True))  # type: ignore[attr-defined]
    await fail_n(refused, 5)  # rejected before any I/O
    assert refused.circuit_state(TENANT) == "closed"
    for handler in (  # all of these are the server being unwell
        answer({}, 429),
        lambda r: httpx.Response(200, content=b"garbage"),
        lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")),
        answer({"version": 2, "scores": []}),
    ):
        chosen, _ = tripping(handler, Clock())
        await fail_n(chosen, 3)
        assert chosen.circuit_state(TENANT) == "open"


async def test_a_success_resets_the_failure_count() -> None:
    clock = Clock()
    ok = {"version": 1, "scores": []}
    sequence = iter([500, 500, 200, 500, 500])
    chosen, _ = tripping(
        lambda r: httpx.Response(next(sequence), json=ok),
        clock,
    )
    await fail_n(chosen, 2)
    assert await chosen.rank("oi", tools(AVAIL), tenant_id=TENANT)
    await fail_n(chosen, 2)
    assert chosen.circuit_state(TENANT) == "closed"  # never 3 in a row


async def test_circuits_are_per_tenant() -> None:
    chosen, calls = tripping(answer({}, 500), Clock())
    await fail_n(chosen, 3)
    with pytest.raises(ToolSelectorError) as other:  # another tenant is still called
        await chosen.rank("oi", tools(AVAIL), tenant_id="another-tenant")
    assert not isinstance(other.value, ToolSelectorCircuitOpenError) and len(calls) == 4


async def test_a_disabled_breaker_always_calls() -> None:
    chosen, calls = tripping(answer({}, 500), Clock(), enabled=False)
    await fail_n(chosen, 10)
    assert len(calls) == 10 and chosen.circuit_state(TENANT) == "closed"


async def test_the_engine_counts_an_open_circuit_and_sends_the_full_list(api: ApiHandle) -> None:
    from conversation_agent.engine.tool_selection import SelectionMonitor

    class Counting:
        def __init__(self) -> None:
            self.outcomes: list[str] = []

        def record_tool_selection(self, *, agent_id: str, agent_version: str, outcome: str) -> None:
            self.outcomes.append(outcome)

    agent = build_agent()
    fields = {n: getattr(agent, n) for n in AgentDefinition.model_fields}
    sel = ToolSelectionSettings(
        enabled=True,
        provider="http",
        max_tools_per_call=1,
        http=HttpSelectorSettings(connection="sel", circuit={"failure_threshold": 1}),
    )
    with_http = AgentDefinition(**{**fields, "tool_selection": sel})
    metrics = Counting()
    monitor = SelectionMonitor("a", "1", metrics)
    shared = tool_selector_for(with_http, guard=guard_for(answer({}, 500)))  # one per runtime
    seen: list[set[str]] = []
    for turn in ("t1", "t2"):
        llm = FakeLLM([text_response("ok")])
        engine, _, _ = build_engine(
            llm,
            api_base_url=api.base_url,
            journal=new_journal(),
            clock=new_clock(),
            agent=with_http,
            tool_selector=shared,
            selection_monitor=monitor,
        )
        await engine.process_turn(IDENTITY, ConversationState(), "disponibilidade", turn)
        seen.append({t.name for t in llm.requests[0].tools})
    assert seen == [{AVAIL, CREATE}, {AVAIL, CREATE}]  # both turns: the full list
    assert metrics.outcomes == ["provider_error", "circuit_open"]


def test_circuit_settings_are_validated() -> None:
    for bad in ({"failure_threshold": 0}, {"open_seconds": 0}, {"half_open_probes": 0}):
        with pytest.raises(ValidationError):
            HttpSelectorSettings(connection="sel", circuit=bad)
    assert HttpSelectorSettings(connection="sel").circuit.enabled  # on by default
