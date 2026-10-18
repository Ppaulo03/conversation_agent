"""Tracing without a backend: spans with a shared trace id, parent links and timings.

A SPAN is "this step, how long it took, how it ended" under one TRACE ID the runtime mints where
an inbound event enters and carries to the reply it caused (webhook -> turn -> LLM calls -> tool
calls -> outbox send). The set of spans is the structure of one conversation turn.

This module only produces spans; a `Tracer` decides where they go (a JSON log line, memory for
tests, later an OpenTelemetry exporter). With no tracer configured, spans cost almost nothing.
Attributes are scrubbed like logs (scalars only, redacted, capped): a span is not a place for
content.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from conversation_agent.core.observability import bind, current
from conversation_agent.core.redaction import redact

MAX_ATTRIBUTES = 12
MAX_TEXT = 200


@dataclass(frozen=True)
class SpanRecord:
    trace_id: str
    span_id: str
    parent_id: str | None
    name: str
    started_at: datetime
    duration_ms: float
    status: str  # ok | error
    error_type: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpanStart:
    trace_id: str
    span_id: str
    parent_id: str | None
    name: str
    started_at: datetime
    attributes: dict[str, Any] = field(default_factory=dict)


class Tracer(Protocol):
    def on_end(self, span: SpanRecord) -> None: ...


class _NullTracer:
    def on_end(self, span: SpanRecord) -> None:
        return None


_TRACER: Tracer = _NullTracer()
_SPAN: ContextVar[str | None] = ContextVar("current_span_id", default=None)


def configure_tracer(tracer: Tracer | None) -> None:
    """Where finished spans go (None: nowhere). Called once by the composition root."""
    global _TRACER
    _TRACER = tracer or _NullTracer()


def new_trace_id() -> str:
    return uuid.uuid4().hex


def trace_id_for(turn_id: str) -> str:
    """The trace of work in progress, or the legacy per-turn id when none was minted (rows that
    predate tracing): deterministic, so a replayed turn derives the same value."""
    return str(current().get("trace_id") or f"trace-{turn_id}")


def _clean(attributes: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in list(attributes.items())[:MAX_ATTRIBUTES]:
        if isinstance(value, bool) or value is None or isinstance(value, int | float):
            out[key] = value
        else:
            out[key] = redact(str(value))[:MAX_TEXT]
    return out


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[None]:
    """Times the block as a span of the current trace (minting a trace id if there is none)."""
    parent = _SPAN.get()
    span_id = uuid.uuid4().hex[:16]
    trace_id = current().get("trace_id") or new_trace_id()
    started_at, started = datetime.now(UTC), time.monotonic()
    status, error_type = "ok", None
    cleaned = _clean(attributes)
    token = _SPAN.set(span_id)
    with suppress(Exception):
        on_start = getattr(_TRACER, "on_start", None)
        if on_start is not None:
            on_start(
                SpanStart(
                    trace_id=trace_id,
                    span_id=span_id,
                    parent_id=parent,
                    name=name,
                    started_at=started_at,
                    attributes=cleaned,
                )
            )
    try:
        with bind(trace_id=trace_id):
            yield
    except BaseException as exc:
        status, error_type = "error", type(exc).__name__
        raise
    finally:
        _SPAN.reset(token)
        with suppress(Exception):  # a tracer never changes what the work does
            _TRACER.on_end(
                SpanRecord(
                    trace_id=trace_id,
                    span_id=span_id,
                    parent_id=parent,
                    name=name,
                    started_at=started_at,
                    duration_ms=(time.monotonic() - started) * 1000,
                    status=status,
                    error_type=error_type,
                    attributes=cleaned,
                )
            )
