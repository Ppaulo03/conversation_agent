"""Retention windows and erasure results (DESIGN §38).

What the runtime keeps about people, and for how long:

  messages           the text of inbound/outbound messages and of a turn's user text
  conversation state the conversation's history and open flows (the operational row stays)
  journal payloads   the replay payloads of finished turns (after this, hashes and metadata only)
  media references   the references to attachments

Rows are REDACTED, not deleted, whenever something else still needs them to exist: an inbox row is
the dedupe record of a delivered event, an outbox row the idempotency record of a send, a tool
invocation the ledger of an external effect. Erasing a contact goes further: everything that names
the contact is removed or replaced by a non-reversible reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field


class RetentionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message_days: int = Field(default=90, ge=1)
    conversation_state_days: int = Field(default=180, ge=1)
    journal_payload_days: int = Field(default=7, ge=1)  # DESIGN: journal_replay_payload_retention
    media_reference_days: int = Field(default=30, ge=1)


@dataclass(frozen=True)
class RetentionResult:
    """What a retention run did (or, with `dry_run`, would do): counts only."""

    dry_run: bool
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


@dataclass(frozen=True)
class ErasureResult:
    status: str  # erased | blocked | nothing_found
    counts: dict[str, int] = field(default_factory=dict)
    blockers: tuple[str, ...] = ()  # why it did not run: what must finish (or be forced) first
    reference: str = ""  # the non-reversible reference the contact's rows now carry


@dataclass(frozen=True)
class ContactFootprint:
    """Where a contact's data lives: counts per table, found through (tenant_id, contact_id)."""

    counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())
