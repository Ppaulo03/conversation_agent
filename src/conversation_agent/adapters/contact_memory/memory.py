from __future__ import annotations

from conversation_agent.core.contact_text import clean, same_fact
from conversation_agent.ports.contact_memory import (
    ContactFact,
    ContactProfile,
    MemoryLimits,
    MemoryResult,
)


class InMemoryContactMemory:
    """Process memory: for tests and single-process demos (nothing survives a restart)."""

    def __init__(self) -> None:
        self._names: dict[tuple[str, str], str] = {}
        self._facts: dict[tuple[str, str], list[ContactFact]] = {}
        self._seq: dict[tuple[str, str], int] = {}

    def _profile(self, key: tuple[str, str]) -> ContactProfile:
        return ContactProfile(self._names.get(key), tuple(self._facts.get(key, ())))

    async def get(self, tenant_id: str, contact_id: str) -> ContactProfile:
        return self._profile((tenant_id, contact_id))

    async def set_name(
        self, tenant_id: str, contact_id: str, name: str, limits: MemoryLimits
    ) -> MemoryResult:
        key = (tenant_id, contact_id)
        cleaned = clean(name, limits.max_name_chars)
        if cleaned is None:
            return MemoryResult("invalid", self._profile(key))
        self._names[key] = cleaned
        return MemoryResult("ok", self._profile(key))

    async def remember(
        self, tenant_id: str, contact_id: str, text: str, limits: MemoryLimits
    ) -> MemoryResult:
        key = (tenant_id, contact_id)
        cleaned = clean(text, limits.max_fact_chars)
        if cleaned is None:
            return MemoryResult("invalid", self._profile(key))
        facts = self._facts.setdefault(key, [])
        for existing in facts:
            if same_fact(existing.text, cleaned):
                return MemoryResult("duplicate", self._profile(key), existing)
        if len(facts) >= limits.max_facts:
            return MemoryResult("full", self._profile(key))
        self._seq[key] = self._seq.get(key, 0) + 1
        fact = ContactFact(f"f{self._seq[key]}", cleaned)
        facts.append(fact)
        return MemoryResult("ok", self._profile(key), fact)

    async def forget(self, tenant_id: str, contact_id: str, fact_id: str) -> MemoryResult:
        key = (tenant_id, contact_id)
        facts = self._facts.get(key, [])
        for existing in facts:
            if existing.id == fact_id:
                facts.remove(existing)
                return MemoryResult("ok", self._profile(key), existing)
        return MemoryResult("not_found", self._profile(key))

    async def erase(self, tenant_id: str, contact_id: str) -> bool:
        key = (tenant_id, contact_id)
        had = key in self._names or bool(self._facts.get(key))
        self._names.pop(key, None)
        self._facts.pop(key, None)
        self._seq.pop(key, None)  # erasure forgets the person: ids may start over
        return had
