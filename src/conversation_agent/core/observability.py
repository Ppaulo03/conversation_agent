"""The context every log line, metric label and LLM usage record shares (Phase 11).

One mechanism, not four: the runtime BINDS what it is working on (tenant, agent version, the
conversation, the turn, ...) for the duration of that work, and anything that observes (the JSON
logger, the LLM usage recorder, the tracer) READS it. Nothing has to be threaded through call
signatures, and a log line written deep inside a tool provider still says which turn it belongs to.

The set of fields is closed on purpose: a stable schema is what makes logs queryable, and a closed
set cannot grow a field that carries content. The conversation is named by `conversation_ref`, a
non-reversible reference (the raw id can embed a phone number): the same reference the audit trail
uses, so an operator can compute it for a known conversation and find its lines.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any

from conversation_agent.core.models.audit import subject_ref

FIELDS: frozenset[str] = frozenset(
    {
        "tenant_id",
        "agent_id",
        "agent_version",
        "channel_id",
        "conversation_ref",
        "turn_id",
        "invocation_id",
        "outbox_id",
        "event_id",
        "trace_id",
        "purpose",  # why an LLM call is made: agent | confirmation | flow | transcription | ...
        "component",
    }
)

_CONTEXT: ContextVar[Mapping[str, Any]] = ContextVar(
    "observability_context", default=MappingProxyType({})
)


def conversation_ref(tenant_id: str, conversation_id: str) -> str:
    """The reference to a conversation used in logs and audit (never the raw id)."""
    return subject_ref(tenant_id, conversation_id)[:16]


def current() -> dict[str, Any]:
    return dict(_CONTEXT.get())


@contextmanager
def bind(**fields: Any) -> Iterator[None]:
    """Adds `fields` to the context for the duration of the block (None values are skipped).
    Unknown names are a programming error: the schema is closed."""
    unknown = set(fields) - FIELDS
    if unknown:
        raise ValueError(f"unknown observability field(s): {sorted(unknown)}")
    merged = {**_CONTEXT.get(), **{k: v for k, v in fields.items() if v is not None}}
    token = _CONTEXT.set(merged)
    try:
        yield
    finally:
        _CONTEXT.reset(token)
