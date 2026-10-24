"""The shipped migrations are one line, numbered 0001, 0002, ... with no repeat and no hole. The migrator applies by
file name and refuses a migration that sorts before one already applied (a gap in the history), so a new file that
reuses a taken number cannot be applied to any database that has the later one: 0.6.0 shipped like that."""

import re

from conversation_agent.adapters.postgres.migrator import _files


def test_migration_numbers_are_unique_and_contiguous() -> None:
    numbers = [int(re.match(r"(\d{4})_", name).group(1)) for name in _files()]  # type: ignore[union-attr]
    assert numbers == list(range(1, len(numbers) + 1)), (
        "migrations must be numbered 0001..N with no repeat: a new one takes the next number, "
        "even when another branch also added one"
    )
