"""A choice named inside another choice's name ("tennis" in "beach tennis") is not ambiguous."""

from __future__ import annotations

import pytest

from conversation_agent.core.definitions.flow import SlotDefinition
from conversation_agent.engine.flow_understanding import _enum_value, fold

SPORT = SlotDefinition(
    name="sport",
    type="enum",
    prompt="?",
    choices={
        "futsal": ("futsal", "futebol"),
        "tennis": ("tenis", "tennis"),
        "beach_tennis": ("beach tennis", "beach"),
    },
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("na verdade prefiro beach tennis", "beach_tennis"),  # "tennis" is inside "beach tennis"
        ("quero jogar tennis", "tennis"),
        ("beach", "beach_tennis"),
        ("futsal", "futsal"),
        ("tennis ou beach tennis", None),  # two real answers: still ambiguous
        ("futsal e tenis", None),
        ("nada disso", None),
    ],
)
def test_the_longest_name_wins_but_two_answers_stay_ambiguous(
    text: str, expected: str | None
) -> None:
    assert _enum_value(SPORT, fold(text)) == expected
