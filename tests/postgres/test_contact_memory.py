"""ContactMemory contract, run against BOTH adapters: they must not disagree on what a valid fact
is, when memory is full, or what an id means after a forget."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from conversation_agent.adapters.contact_memory.memory import InMemoryContactMemory
from conversation_agent.adapters.postgres.contact_memory import PostgresContactMemory
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.ports.contact_memory import ContactMemory, MemoryLimits

T, C = "tenant", "contact-1"
LIMITS = MemoryLimits(max_facts=3, max_fact_chars=40, max_name_chars=10)


@pytest.fixture(params=["memory", "postgres"])
def backend(request: pytest.FixtureRequest) -> str | None:
    """None for the in-memory adapter, else the DSN (this sync fixture skips when there is no
    database, and only for the postgres case)."""
    return None if request.param == "memory" else str(request.getfixturevalue("pg_dsn"))


@pytest.fixture
async def memory(backend: str | None) -> AsyncIterator[ContactMemory]:
    if backend is None:
        yield InMemoryContactMemory()
        return
    db = await PostgresDatabase.connect(backend, max_size=10)
    try:
        await db.pool.execute("TRUNCATE contact_profiles")
        yield PostgresContactMemory(db)
    finally:
        await db.close()


async def test_a_new_contact_has_an_empty_profile(memory: ContactMemory) -> None:
    assert (await memory.get(T, C)).empty


async def test_a_name_and_facts_are_kept_and_read_back(memory: ContactMemory) -> None:
    assert (await memory.set_name(T, C, "  Ana  ", LIMITS)).status == "ok"
    added = await memory.remember(T, C, "Treina de   manhã", LIMITS)
    assert added.status == "ok" and added.fact is not None and added.fact.text == "Treina de manhã"
    profile = await memory.get(T, C)
    assert profile.display_name == "Ana"
    assert [(f.id, f.text) for f in profile.facts] == [("f1", "Treina de manhã")]


async def test_memory_belongs_to_the_tenant_and_the_contact(memory: ContactMemory) -> None:
    await memory.remember(T, C, "gosta de café", LIMITS)
    assert (await memory.get(T, "contact-2")).empty
    assert (await memory.get("other-tenant", C)).empty


@pytest.mark.parametrize("text", ["", "   ", "x" * 41, "linha\x00nula", "bell\x07", "zero​width"])
async def test_text_that_is_not_a_short_sentence_is_refused(
    memory: ContactMemory, text: str
) -> None:
    result = await memory.remember(T, C, text, LIMITS)
    assert result.status == "invalid" and (await memory.get(T, C)).empty


async def test_a_name_over_its_limit_is_refused_not_cut(memory: ContactMemory) -> None:
    assert (await memory.set_name(T, C, "a" * 11, LIMITS)).status == "invalid"
    assert (await memory.get(T, C)).display_name is None


async def test_newlines_are_collapsed_so_a_fact_is_one_line(memory: ContactMemory) -> None:
    result = await memory.remember(T, C, "primeira\nsegunda", LIMITS)
    assert result.fact is not None and result.fact.text == "primeira segunda"


async def test_the_same_fact_ignoring_case_and_spacing_is_not_added_twice(
    memory: ContactMemory,
) -> None:
    first = await memory.remember(T, C, "Treina de manhã", LIMITS)
    again = await memory.remember(T, C, "treina  DE manhã", LIMITS)
    assert again.status == "duplicate" and again.fact == first.fact
    assert len((await memory.get(T, C)).facts) == 1


async def test_a_full_memory_refuses_and_forgets_nothing(memory: ContactMemory) -> None:
    for text in ("um", "dois", "tres"):
        assert (await memory.remember(T, C, text, LIMITS)).status == "ok"
    refused = await memory.remember(T, C, "quatro", LIMITS)
    assert refused.status == "full"
    assert [f.text for f in (await memory.get(T, C)).facts] == ["um", "dois", "tres"]


async def test_a_forgotten_fact_leaves_room_and_its_id_is_never_reused(
    memory: ContactMemory,
) -> None:
    for text in ("um", "dois", "tres"):
        await memory.remember(T, C, text, LIMITS)
    assert (await memory.forget(T, C, "f2")).status == "ok"
    added = await memory.remember(T, C, "quatro", LIMITS)
    assert added.fact is not None and added.fact.id == "f4"
    assert [f.id for f in (await memory.get(T, C)).facts] == ["f1", "f3", "f4"]


async def test_forgetting_what_is_not_there_changes_nothing(memory: ContactMemory) -> None:
    await memory.remember(T, C, "um", LIMITS)
    assert (await memory.forget(T, C, "f9")).status == "not_found"
    assert (await memory.forget(T, "stranger", "f1")).status == "not_found"
    assert len((await memory.get(T, C)).facts) == 1


async def test_erasing_removes_everything_and_says_whether_there_was_something(
    memory: ContactMemory,
) -> None:
    await memory.set_name(T, C, "Ana", LIMITS)
    await memory.remember(T, C, "um", LIMITS)
    assert await memory.erase(T, C) is True
    assert (await memory.get(T, C)).empty
    assert await memory.erase(T, C) is False


async def test_concurrent_writes_to_one_contact_never_lose_a_fact_or_exceed_the_limit(
    memory: ContactMemory,
) -> None:
    limits = MemoryLimits(max_facts=5, max_fact_chars=40, max_name_chars=10)
    results = await asyncio.gather(*(memory.remember(T, C, f"fato {i}", limits) for i in range(12)))
    assert sum(r.status == "ok" for r in results) == 5
    assert sum(r.status == "full" for r in results) == 7
    profile = await memory.get(T, C)
    assert len(profile.facts) == 5 and len({f.id for f in profile.facts}) == 5
