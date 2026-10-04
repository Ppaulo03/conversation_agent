"""Meta-test: every chaos case ROADMAP assigns to a delivered phase has a named test."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# ROADMAP "Chaos gates": phase in which each case becomes mandatory.
REQUIRED_BY_PHASE = {
    2: ["C04", "C05", "C06", "C07", "C08", "C09", "C10", "C11", "C14", "C15", "C16"],
    3: ["C01", "C02", "C03", "C13"],
    4: ["C12"],
}
DELIVERED_PHASES = (2, 3, 4)


def named_tests() -> set[str]:
    names: set[str] = set()
    for path in (ROOT / "tests").rglob("test_*.py"):
        names.update(re.findall(r"def (test_C\d\d)_", path.read_text(encoding="utf-8")))
    return names


@pytest.mark.parametrize(
    "case", [c for phase in DELIVERED_PHASES for c in REQUIRED_BY_PHASE[phase]]
)
def test_every_required_chaos_case_has_a_named_test(case: str) -> None:
    assert f"test_{case}" in named_tests(), f"chaos case {case} has no test_{case}_* test"
