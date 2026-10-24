"""The `jev` tool selector: one `choice` question over the candidate tools, probabilities as scores."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from conversation_agent.adapters.connections.static import StaticConnectionResolver
from conversation_agent.adapters.secrets.providers import InMemorySecretProvider
from conversation_agent.adapters.tool_selection.factory import tool_selector_for
from conversation_agent.adapters.tool_selection.jev import QUESTION, JevToolSelector
from conversation_agent.adapters.tools.guard import GuardedTransport
from conversation_agent.core.definitions.agent import AgentDefinition
from conversation_agent.core.definitions.tool_selection import (
    JevSelectorSettings,
    ToolSelectionSettings,
)
from conversation_agent.core.errors import ToolSelectorCircuitOpenError, ToolSelectorError
from conversation_agent.core.models.connections import AuthSpec, ResolvedConnection
from conversation_agent.core.models.llm import LLMToolDefinition
from support.builders import IDENTITY
from vertical_slice.definitions import build_agent

TENANT = IDENTITY.tenant_id
CANDIDATES = tuple(
    LLMToolDefinition(name=n, description=f"{n} descricao", input_schema={})
    for n in ("agenda_create", "agenda_cancel", "clientes_find")
)


async def public_dns(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


def guard_for(handler: Any, *, token: str | None = "sk-test") -> GuardedTransport:
    connection = ResolvedConnection(
        connection_id="jev",
        base_url="https://api.typesafe.ai",
        allowed_hosts=("api.typesafe.ai",),
        auth=AuthSpec(secret_ref="key") if token else None,
    )
    return GuardedTransport(
        StaticConnectionResolver({"jev": connection}),
        InMemorySecretProvider({(TENANT, "key"): token}) if token else None,
        httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False),
        public_dns,
    )


def answer(probabilities: dict[str, float] | None, choice: str = "agenda_create") -> dict[str, Any]:
    body: dict[str, Any] = {"type": "choice", "choice": choice, "confidence": 0.9}
    if probabilities is not None:
        body["probabilities"] = probabilities
    return {"answers": {QUESTION: body}, "usage": {"input_tokens": 10}}


def make(handler: Any, **over: Any) -> JevToolSelector:
    return JevToolSelector(
        guard_for(handler), JevSelectorSettings(connection="jev", **over), top_n=8
    )


async def test_request_shape_and_rescaled_scores() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json=answer({"agenda_create": 0.6, "agenda_cancel": 0.3, "clientes_find": 0.1})
        )

    scores = await make(handler).rank("quero marcar corte", CANDIDATES, tenant_id=TENANT)
    request = seen[0]
    assert (
        request.url.path == "/v1/systemone"
    )  # the IP is pinned; the Host header names the service
    assert request.headers["host"] == "api.typesafe.ai"
    assert request.headers["authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body["model"] == "jev-latest" and body["state"] == "quero marcar corte"
    question = body["questions"][QUESTION]
    assert question["type"] == "choice"
    assert question["criteria"] == {t.name: t.description for t in CANDIDATES}
    by_name = {s.name: s.score for s in scores}
    assert by_name["agenda_create"] == 1.0
    assert by_name["agenda_cancel"] == pytest.approx(0.5)
    assert by_name["clientes_find"] == pytest.approx(1 / 6)


async def test_only_the_winner_known() -> None:
    selector = make(lambda r: httpx.Response(200, json=answer(None, "agenda_cancel")))
    by_name = {s.name: s.score for s in await selector.rank("x", CANDIDATES, tenant_id=TENANT)}
    assert by_name == {"agenda_cancel": 1.0, "agenda_create": 0.0, "clientes_find": 0.0}


async def test_invented_names_are_ignored() -> None:
    selector = make(
        lambda r: httpx.Response(200, json=answer({"agenda_create": 0.5, "ghost": 0.5}))
    )
    by_name = {s.name: s.score for s in await selector.rank("x", CANDIDATES, tenant_id=TENANT)}
    assert "ghost" not in by_name and by_name["agenda_create"] == 1.0


@pytest.mark.parametrize(
    "payload",
    [
        {"answers": {}},
        {"nope": 1},
        answer({"agenda_create": 1.5}),
        answer({"agenda_create": 0.0}),
        answer({"ghost": 1.0}),
        answer({"agenda_create": True}),
    ],
)
async def test_unusable_answer_is_a_failure(payload: dict[str, Any]) -> None:
    selector = make(lambda r: httpx.Response(200, json=payload))
    with pytest.raises(ToolSelectorError):
        await selector.rank("x", CANDIDATES, tenant_id=TENANT)


@pytest.mark.parametrize("status", [429, 500, 529])
async def test_overload_statuses_open_the_circuit(status: int) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status)

    selector = make(handler, circuit={"failure_threshold": 2})
    for _ in range(2):
        with pytest.raises(ToolSelectorError):
            await selector.rank("x", CANDIDATES, tenant_id=TENANT)
    with pytest.raises(ToolSelectorCircuitOpenError):
        await selector.rank("x", CANDIDATES, tenant_id=TENANT)
    assert calls == 2


async def test_bad_key_does_not_trip_the_breaker() -> None:
    selector = make(lambda r: httpx.Response(401), circuit={"failure_threshold": 1})
    for _ in range(3):
        with pytest.raises(ToolSelectorError) as info:
            await selector.rank("x", CANDIDATES, tenant_id=TENANT)
        assert not isinstance(info.value, ToolSelectorCircuitOpenError)


def test_settings_and_factory() -> None:
    with pytest.raises(ValidationError):
        ToolSelectionSettings(provider="jev")
    with pytest.raises(ValidationError):
        ToolSelectionSettings(provider="rules", jev=JevSelectorSettings(connection="jev"))
    s = ToolSelectionSettings(
        enabled=True,
        provider="jev",
        max_tools_per_call=3,
        jev=JevSelectorSettings(connection="jev"),
    )
    assert s.jev is not None and s.jev.path == "/v1/systemone" and s.jev.model == "jev-latest"
    base = build_agent()
    fields = {n: getattr(base, n) for n in AgentDefinition.model_fields}
    agent = AgentDefinition(**{**fields, "tool_selection": s})
    with pytest.raises(ValueError, match="GuardedTransport"):
        tool_selector_for(agent)
    chosen = tool_selector_for(agent, guard=guard_for(lambda r: httpx.Response(200)))
    assert isinstance(chosen, JevToolSelector)
