"""A part of the persona that only makes sense when some capability is available to the contact
in this turn (ADR-033). With the port `CapabilityAvailability` an agent can hide a capability from
one contact; the prompt must then stop talking about it too, or the model keeps offering what it can
no longer do. An agent without sections keeps its prompt and its digest exactly as before."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

MAX_SECTION_CHARS = 8000
MAX_SECTIONS = 20


class PersonaSection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    # The section is added after the persona only when at least one of these capabilities is
    # offered to the contact in this turn. All of them must be allowed capabilities of the agent.
    requires_any: tuple[str, ...] = Field(min_length=1, max_length=50)
    text: str = Field(min_length=1, max_length=MAX_SECTION_CHARS)
