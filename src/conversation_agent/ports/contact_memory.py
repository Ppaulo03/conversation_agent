"""What the framework remembers about a CONTACT across conversations (name and short facts).

Conversation state dies with the conversation (a reset, the conversation's retention); this does
not. It belongs to `(tenant_id, contact_id)` and to nothing else: the identity always comes from
the runtime, never from the model. It is data the contact told the agent, so it reaches the prompt
as data, never as instructions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from conversation_agent.core.definitions.memory import MemoryLimits

__all__ = [
    "ContactFact",
    "ContactMemory",
    "ContactProfile",
    "MemoryLimits",
    "MemoryResult",
    "MemoryStatus",
]


@dataclass(frozen=True)
class ContactFact:
    id: str  # "f<n>": the number only grows, an id is never reused after a forget
    text: str


@dataclass(frozen=True)
class ContactProfile:
    display_name: str | None = None
    facts: tuple[ContactFact, ...] = ()

    @property
    def empty(self) -> bool:
        return self.display_name is None and not self.facts


# `invalid`: blank, too long, or not text a person could have said (a control character).
# `full`: the limit was reached; nothing is forgotten silently (the caller tells the contact).
# `duplicate`: the same fact (ignoring case and spacing) is already there; nothing changed.
# `not_found`: `forget` named a fact that is not there.
MemoryStatus = Literal["ok", "duplicate", "invalid", "full", "not_found"]


@dataclass(frozen=True)
class MemoryResult:
    status: MemoryStatus
    profile: ContactProfile
    fact: ContactFact | None = None  # the fact added, or the one already there (duplicate)


class ContactMemory(Protocol):
    async def get(self, tenant_id: str, contact_id: str) -> ContactProfile: ...

    async def set_name(
        self, tenant_id: str, contact_id: str, name: str, limits: MemoryLimits
    ) -> MemoryResult: ...

    async def remember(
        self, tenant_id: str, contact_id: str, text: str, limits: MemoryLimits
    ) -> MemoryResult: ...

    async def forget(self, tenant_id: str, contact_id: str, fact_id: str) -> MemoryResult: ...

    async def erase(self, tenant_id: str, contact_id: str) -> bool:
        """Remove everything remembered about the contact. True if there was something."""
        ...
