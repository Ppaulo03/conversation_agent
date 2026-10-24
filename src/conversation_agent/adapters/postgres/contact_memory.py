from __future__ import annotations

import json
from typing import Any

from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.core.contact_text import clean, same_fact
from conversation_agent.ports.contact_memory import (
    ContactFact,
    ContactProfile,
    MemoryLimits,
    MemoryResult,
)

_LOCK = (
    "SELECT display_name, facts, fact_seq FROM contact_profiles "
    "WHERE tenant_id = $1 AND contact_id = $2 FOR UPDATE"
)


def _profile(display_name: str | None, facts: Any) -> ContactProfile:
    items = json.loads(facts) if isinstance(facts, str) else facts  # codec-dependent jsonb
    return ContactProfile(display_name, tuple(ContactFact(f["id"], f["text"]) for f in items))


class PostgresContactMemory:
    """One transaction per change, serialized per contact by the row lock; the clock is the
    database's (INV-032)."""

    def __init__(self, db: PostgresDatabase) -> None:
        self._db = db

    async def get(self, tenant_id: str, contact_id: str) -> ContactProfile:
        row = await self._db.pool.fetchrow(
            "SELECT display_name, facts FROM contact_profiles "
            "WHERE tenant_id = $1 AND contact_id = $2",
            tenant_id,
            contact_id,
        )
        return ContactProfile() if row is None else _profile(row["display_name"], row["facts"])

    async def _locked(self, conn: Any, tenant_id: str, contact_id: str) -> Any:
        """The contact's row, created empty when missing, and locked until the transaction ends."""
        await conn.execute(
            "INSERT INTO contact_profiles (tenant_id, contact_id) VALUES ($1, $2) "
            "ON CONFLICT DO NOTHING",
            tenant_id,
            contact_id,
        )
        return await conn.fetchrow(_LOCK, tenant_id, contact_id)

    async def set_name(
        self, tenant_id: str, contact_id: str, name: str, limits: MemoryLimits
    ) -> MemoryResult:
        cleaned = clean(name, limits.max_name_chars)
        if cleaned is None:
            return MemoryResult("invalid", await self.get(tenant_id, contact_id))
        async with self._db.pool.acquire() as conn, conn.transaction():
            row = await self._locked(conn, tenant_id, contact_id)
            await conn.execute(
                "UPDATE contact_profiles SET display_name = $3, updated_at = clock_timestamp() "
                "WHERE tenant_id = $1 AND contact_id = $2",
                tenant_id,
                contact_id,
                cleaned,
            )
            return MemoryResult("ok", _profile(cleaned, row["facts"]))

    async def remember(
        self, tenant_id: str, contact_id: str, text: str, limits: MemoryLimits
    ) -> MemoryResult:
        cleaned = clean(text, limits.max_fact_chars)
        if cleaned is None:
            return MemoryResult("invalid", await self.get(tenant_id, contact_id))
        async with self._db.pool.acquire() as conn, conn.transaction():
            row = await self._locked(conn, tenant_id, contact_id)
            profile = _profile(row["display_name"], row["facts"])
            for existing in profile.facts:
                if same_fact(existing.text, cleaned):
                    return MemoryResult("duplicate", profile, existing)
            if len(profile.facts) >= limits.max_facts:
                return MemoryResult("full", profile)
            fact = ContactFact(f"f{row['fact_seq'] + 1}", cleaned)
            facts = (*profile.facts, fact)
            await conn.execute(
                "UPDATE contact_profiles SET facts = $3::jsonb, fact_seq = fact_seq + 1, "
                "updated_at = clock_timestamp() WHERE tenant_id = $1 AND contact_id = $2",
                tenant_id,
                contact_id,
                json.dumps([{"id": f.id, "text": f.text} for f in facts]),
            )
            return MemoryResult("ok", ContactProfile(profile.display_name, facts), fact)

    async def forget(self, tenant_id: str, contact_id: str, fact_id: str) -> MemoryResult:
        async with self._db.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(_LOCK, tenant_id, contact_id)
            if row is None:
                return MemoryResult("not_found", ContactProfile())
            profile = _profile(row["display_name"], row["facts"])
            gone = next((f for f in profile.facts if f.id == fact_id), None)
            if gone is None:
                return MemoryResult("not_found", profile)
            kept = tuple(f for f in profile.facts if f.id != fact_id)
            await conn.execute(
                "UPDATE contact_profiles SET facts = $3::jsonb, updated_at = clock_timestamp() "
                "WHERE tenant_id = $1 AND contact_id = $2",
                tenant_id,
                contact_id,
                json.dumps([{"id": f.id, "text": f.text} for f in kept]),
            )
            return MemoryResult("ok", ContactProfile(profile.display_name, kept), gone)

    async def erase(self, tenant_id: str, contact_id: str) -> bool:
        status = await self._db.pool.execute(
            "DELETE FROM contact_profiles WHERE tenant_id = $1 AND contact_id = $2",
            tenant_id,
            contact_id,
        )
        return int(status.split()[-1]) > 0
