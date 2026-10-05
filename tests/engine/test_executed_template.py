"""Phase 14: the executed-reply template is optional, checked at compile time, and exact."""

from __future__ import annotations

import pytest

from conversation_agent.core.compiler import CompileError, agent_document, compile_manifest
from conversation_agent.core.display import display_value, render_executed
from support.support_domain import manifest

TZ = "America/Sao_Paulo"


def test_datetimes_are_shown_in_the_agents_timezone_not_utc() -> None:
    assert display_value("2026-10-06T13:00:00+00:00", TZ) == "ter 06/10 às 10:00"
    assert display_value("2026-10-06T10:00:00", TZ) == "2026-10-06T10:00:00"  # no zone: untouched
    assert display_value("haircut", TZ) == "haircut" and display_value(30, TZ) == "30"


def test_the_result_wins_over_the_arguments_and_a_missing_field_gives_no_sentence() -> None:
    args = {"court": "Quadra 1", "start_at": "2026-10-06T13:00:00+00:00"}
    assert (
        render_executed(
            "{court} em {start_at} (reserva {id})", args, {"id": "R1", "court": "Q2"}, TZ
        )
        == "Q2 em ter 06/10 às 10:00 (reserva R1)"
    )
    assert render_executed("reserva {id}", args, {}, TZ) is None  # never say half the truth


def _with_template(text: str | None) -> dict:  # type: ignore[type-arg]
    raw = manifest()
    capability = next(c for c in raw["capabilities"] if c.get("confirmation_required"))
    if text is not None:
        capability["executed_template"] = text
    return raw


def test_a_template_naming_an_unknown_field_does_not_compile() -> None:
    with pytest.raises(CompileError, match="EXECUTED_UNKNOWN_FIELD"):
        compile_manifest(_with_template("Feito: {nope}"))


def test_only_capabilities_that_set_it_carry_it_in_the_digest() -> None:
    plain = compile_manifest(_with_template(None))
    assert all("executed_template" not in c for c in agent_document(plain.agent)["capabilities"])
    first = next(
        c
        for c in compile_manifest(_with_template("Pronto.")).agent.capabilities
        if c.executed_template
    )
    assert first.executed_template == "Pronto."
