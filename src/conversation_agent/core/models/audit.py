"""Administrative audit trail (DESIGN §38): who did what to what, never the content.

An entry carries identifiers, counts and short codes. `details` is scrubbed on construction:
scalar values only, bounded, free text redacted, and keys that name content are refused, so an
operation that handles personal data cannot leak it into the trail by accident.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from conversation_agent.core.errors import ConversationAgentError
from conversation_agent.core.redaction import redact

Outcome = Literal["ok", "refused", "failed"]
MAX_DETAILS = 20
MAX_TEXT = 200
_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
# Names that say "this is content": they never belong in the trail.
FORBIDDEN_KEYS = frozenset(
    {"text", "message", "content", "body", "args", "arguments", "email", "phone", "name", "result"}
)


MIN_PSEUDONYM_KEY_BYTES = 32
_PSEUDONYM_KEY: bytes | None = None
_PROCESS_KEY = secrets.token_bytes(32)  # logs only: correlates inside one process, nothing else


class PseudonymKeyError(ConversationAgentError):
    """No (usable) pseudonymisation key: no reference is generated rather than a weak one."""


def configure_pseudonym_key(key: str | bytes | None) -> None:
    """The secret every pseudonym is derived from (deployment-wide, rotatable). At least 32 bytes of
    randomness, e.g. `openssl rand -hex 32`. Without it `subject_ref` refuses to run: an unkeyed
    hash of a phone number or a CPF can be enumerated offline by anyone holding the hash."""
    global _PSEUDONYM_KEY
    if key is None:
        _PSEUDONYM_KEY = None
        return
    raw = key.encode() if isinstance(key, str) else key
    if len(raw) < MIN_PSEUDONYM_KEY_BYTES:
        raise PseudonymKeyError(
            f"the pseudonymisation key needs at least {MIN_PSEUDONYM_KEY_BYTES} bytes"
        )
    _PSEUDONYM_KEY = raw


def _derive(master: bytes, domain: bytes, tenant_id: str, value: str) -> str:
    tenant_key = hmac.new(master, b"tenant\x00" + tenant_id.encode(), hashlib.sha256).digest()
    mac = hmac.new(tenant_key, domain + b"\x00" + value.encode(), hashlib.sha256)
    return mac.hexdigest()[:32]


def subject_ref(tenant_id: str, value: str) -> str:
    """A stable reference to a person-level identifier (a contact): HMAC-SHA256 under the
    deployment's key, derived per tenant. The same contact always maps to the same reference (so an
    erasure can be proven when asked) but, without the key, the reference cannot be tested against
    guesses of the identifier. Raises `PseudonymKeyError` when no key is configured."""
    if _PSEUDONYM_KEY is None:
        raise PseudonymKeyError(
            "no pseudonymisation key configured (configure_pseudonym_key / PSEUDONYM_KEY)"
        )
    return _derive(_PSEUDONYM_KEY, b"subject", tenant_id, value)


def log_ref(tenant_id: str, value: str) -> str:
    """A short correlation reference for LOGS. Keyed like `subject_ref` when a key is configured;
    otherwise by a random per-process key (it correlates lines within one process and cannot be
    reversed or compared across processes). Never raises: logging must not depend on the key."""
    return _derive(_PSEUDONYM_KEY or _PROCESS_KEY, b"log", tenant_id, value)


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
