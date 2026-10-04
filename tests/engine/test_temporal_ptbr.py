"""Deterministic pt-BR date/time/duration normalisation (ROADMAP Phase 4)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from conversation_agent.core.temporal_ptbr import (
    format_local,
    parse_date,
    parse_duration,
    parse_time,
    reference_date,
)

MONDAY = date(2026, 10, 5)  # the turn's reference date in every test below


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("hoje", date(2026, 10, 5)),
        ("amanhã", date(2026, 10, 6)),
        ("Amanha", date(2026, 10, 6)),
        ("depois de amanhã", date(2026, 10, 7)),  # must not be read as plain "amanhã"
        ("terça", date(2026, 10, 6)),
        ("na terça-feira", date(2026, 10, 6)),
        ("quinta", date(2026, 10, 8)),
        ("sábado", date(2026, 10, 10)),
        ("segunda", date(2026, 10, 12)),  # said on a Monday: NEXT Monday, never today
        ("próxima sexta", date(2026, 10, 9)),
        ("dia 15", date(2026, 10, 15)),
        ("dia 3", date(2026, 11, 3)),  # already passed this month -> next month
        ("dia 5", date(2026, 10, 5)),  # today counts
        ("15/10", date(2026, 10, 15)),
        ("15/10/2026", date(2026, 10, 15)),
        ("15-10-26", date(2026, 10, 15)),
        ("2/10", date(2027, 10, 2)),
        ("15 de outubro", date(2026, 10, 15)),
        ("3 de out", date(2027, 10, 3)),  # passed this year -> next year
        ("20 de dezembro", date(2026, 12, 20)),
        ("2026-10-15", date(2026, 10, 15)),
        ("quero cortar o cabelo na quinta às 15h", date(2026, 10, 8)),  # inside a sentence
    ],
)
def test_dates(text: str, expected: date) -> None:
    assert parse_date(text, MONDAY) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "oi tudo bem",
        "semana que vem",
        "dia 31/02",
        "32/10",
        "30 de fevereiro",
        "preciso de ajuda",
    ],
)
def test_unparseable_or_impossible_dates_are_none_never_guessed(text: str) -> None:
    assert parse_date(text, MONDAY) is None


def test_a_bare_day_that_does_not_exist_is_never_invented() -> None:
    assert parse_date("dia 31", date(2026, 1, 15)) == date(2026, 1, 31)
    assert parse_date("dia 31", date(2026, 2, 3)) == date(
        2026, 3, 31
    )  # Feb has no 31st: March does
    assert parse_date("dia 30", date(2026, 1, 31)) is None  # passed; February has no 30th


def test_december_rolls_into_next_january() -> None:
    assert parse_date("dia 2", date(2026, 12, 20)) == date(2027, 1, 2)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("15h", time(15, 0)),
        ("15h30", time(15, 30)),
        ("15:30", time(15, 30)),
        ("10 horas", time(10, 0)),
        ("às 10", time(10, 0)),
        ("às 15", time(15, 0)),
        ("às 10 e meia", time(10, 30)),
        ("3 da tarde", time(15, 0)),
        ("às 3 da tarde", time(15, 0)),
        ("9 da manhã", time(9, 0)),
        ("8 da noite", time(20, 0)),
        ("12 da noite", time(0, 0)),
        ("meio-dia", time(12, 0)),
        ("meio dia", time(12, 0)),
        ("meia noite", time(0, 0)),
        ("três da tarde", time(15, 0)),
        ("amanhã às 15h", time(15, 0)),
        ("terça 10h", time(10, 0)),
        ("dia 15 às 10h", time(10, 0)),  # "15" is the day, not the hour
    ],
)
def test_times(text: str, expected: time) -> None:
    assert parse_time(text) == expected


@pytest.mark.parametrize(
    "text", ["às 3", "3h", "às 5", "3:30", "amanhã", "quero agendar", "dia 15", "25h", "10:99", ""]
)
def test_ambiguous_or_absent_times_are_none(text: str) -> None:
    assert parse_time(text) is None


@pytest.mark.parametrize(
    ("text", "minutes"),
    [
        ("meia hora", 30),
        ("30 minutos", 30),
        ("45 min", 45),
        ("1 hora", 60),
        ("uma hora", 60),
        ("2 horas", 120),
        ("duas horas", 120),
        ("1h30", 90),
        ("1h 30", 90),
        ("uma hora e meia", 90),
        ("2 horas e meia", 150),
        ("1 hora e 15 minutos", 75),
        ("90 minutos", 90),
    ],
)
def test_durations(text: str, minutes: int) -> None:
    assert parse_duration(text) == minutes


@pytest.mark.parametrize("text", ["", "bastante tempo", "amanhã", "às 15h"])
def test_no_duration_is_none(text: str) -> None:
    assert parse_duration(text) is None


def test_today_is_the_reference_time_in_the_agents_timezone_not_utc() -> None:
    late_utc = datetime(2026, 10, 6, 1, 30, tzinfo=UTC)  # still Monday evening in São Paulo
    assert reference_date(late_utc, "America/Sao_Paulo") == date(2026, 10, 5)
    assert reference_date(late_utc, "UTC") == date(2026, 10, 6)


def test_relative_dates_never_depend_on_the_wall_clock() -> None:
    """Same expression, same reference date -> same answer, today or in a year (replay safe)."""
    reference = date(2026, 10, 5)
    first = parse_date("amanhã", reference)
    assert parse_date("amanhã", reference + timedelta(days=0)) == first
    assert parse_date("amanhã", reference + timedelta(days=365)) != first


def test_presentation_is_local_and_deterministic() -> None:
    moment = datetime(2026, 10, 6, 13, 0, tzinfo=UTC)  # 10:00 in São Paulo
    assert format_local(moment, "America/Sao_Paulo") == ("ter 06/10", "10:00")
