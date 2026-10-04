"""Administrative audit trail (DESIGN §38): who did what to what, never the content.

An entry carries identifiers, counts and short codes. `details` is scrubbed on construction:
scalar values only, bounded, free text redacted, and keys that name content are refused, so an
operation that handles personal data cannot leak it into the trail by accident.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from conversation_agent.core.redaction import redact

Outcome = Literal["ok", "refused", "failed"]
MAX_DETAILS = 20
MAX_TEXT = 200
_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
# Names that say "this is content": they never belong in the trail.
FORBIDDEN_KEYS = frozenset(
    {"text", "message", "content", "body", "args", "arguments", "email", "phone", "name", "result"}
)


def subject_ref(tenant_id: str, value: str) -> str:
    """A stable, non-reversible reference to a person-level identifier (a contact): enough to
    prove an erasure happened for a given contact when asked, without storing the identifier."""
    return hashlib.sha256(f"{tenant_id}\x00{value}".encode()).hexdigest()[:32]


class AuditEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=200)
    actor: str = Field(min_length=1, max_length=200)
    action: str = Field(pattern=r"^[a-z][a-z0-9_.]*$", max_length=80)
    subject_type: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=40)
    subject_id: str = Field(min_length=1, max_length=200)
    outcome: Outcome = "ok"
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("details")
    @classmethod
    def _scrub(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > MAX_DETAILS:
            raise ValueError(f"at most {MAX_DETAILS} audit details")
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if not _KEY.fullmatch(key) or key in FORBIDDEN_KEYS:
                raise ValueError(f"audit detail key {key!r} is not allowed")
            if isinstance(item, bool) or item is None or isinstance(item, int | float):
                clean[key] = item
            elif isinstance(item, str):
                clean[key] = redact(item)[:MAX_TEXT]
            elif isinstance(item, list | tuple) and len(item) <= MAX_DETAILS:
                if not all(isinstance(i, str | int | bool) for i in item):
                    raise ValueError(f"audit detail {key!r} must be a list of scalars")
                clean[key] = [redact(i)[:MAX_TEXT] if isinstance(i, str) else i for i in item]
            else:
                raise ValueError(f"audit detail {key!r} must be a scalar or a short list")
        return clean


class AuditRecord(AuditEntry):
    id: int
    occurred_at: datetime
