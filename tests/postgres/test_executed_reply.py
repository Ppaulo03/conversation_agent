"""Phase 14 (POC item 13): what the contact is told after a confirmed action cannot contradict
what happened. With an `executed_template` no model is involved; without one the model is told
the action HAS been executed, whatever its persona says about proposals."""

from __future__ import annotations

from pathlib import Path

import pytest

import vertical_slice.wiring as wiring
from conftest import ApiHandle
from conversation_agent.adapters.clock import SystemClock
from conversation_agent.adapters.llm.fake import FakeLLM, text_response, tool_call_response
from conversation_agent.adapters.manifest.yaml_loader import load_manifest_file
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.compiler import compile_manifest
from conversation_agent.ports.clock import Clock
from postgres.test_runtime_serve import BOOKING, build


@pytest.fixture
def clock() -> Clock:
    """The real clock (see test_runtime_serve): coordination time is the database's."""
    return SystemClock("America/Sao_Paulo")


MANIFEST = Path(wiring.__file__).with_name("agent.yaml")
TEMPLATE = "Pronto! Reserva {booking_id} de {service_id} confirmada para {start_at}."


def compiled_with(template: str | None) -> object:
    raw = load_manifest_file(MANIFEST)
    for capability in raw["capabilities"]:
        if capability["name"] == "scheduling.create" and template is not None:
            capability["executed_template"] = template
    return compile_manifest(raw)


async def book(db: PostgresDatabase, clock: Clock, api: ApiHandle, compiled: object, llm: FakeLLM):  # type: ignore[no-untyped-def]
    out: list[str] = []
    runtime, channel = build(db, clock, api, llm, out, compiled)  # type: ignore[arg-type]
    await runtime.receive(channel.inbound("quero terça às 10h"))
    await runtime.drain()
    await runtime.receive(channel.inbound("sim"))
    await runtime.drain()
    return out


async def test_a_template_reports_the_real_result_with_no_model_call(
    db: PostgresDatabase,
    clock: Clock,
    api: ApiHandle,
) -> None:
    llm = FakeLLM(
        [tool_call_response("scheduling__create", BOOKING), text_response("Posso reservar.")]
    )  # a third response is not scripted: composing with the model would fail loudly
    out = await book(db, clock, api, compiled_with(TEMPLATE), llm)
    (booking_id,) = api.state.bookings
    assert (
        out[-1]
        == f"bot> Pronto! Reserva {booking_id} de haircut confirmada para ter 06/10 às 10:00."
    )
    assert llm.calls == 2  # the proposal turn only


async def test_without_a_template_the_model_is_told_the_action_was_executed(
    db: PostgresDatabase,
    clock: Clock,
    api: ApiHandle,
) -> None:
    llm = FakeLLM(
        [
            tool_call_response("scheduling__create", BOOKING),
            text_response("Posso reservar."),
            text_response("Reservado!"),
        ]
    )
    out = await book(db, clock, api, compiled_with(None), llm)
    assert out[-1] == "bot> Reservado!"
    system = llm.requests[-1].system
    assert "has just been executed" in system and "'success'" in system
    assert "never say it is pending" in system
