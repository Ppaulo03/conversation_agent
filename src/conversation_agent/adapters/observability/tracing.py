from __future__ import annotations

import logging

from conversation_agent.core.tracing import SpanRecord

log = logging.getLogger("conversation_agent.trace")


class LogTracer:
    """One structured log line per finished span (`event: "span"`), on the JSON logger: the trace
    id, span id, parent, name, duration and status become fields, so any log backend can rebuild
    the trace of a turn with `trace_id = ...` and sort by time. The bound context (tenant, turn,
    agent version) rides along."""

    def on_end(self, span: SpanRecord) -> None:
        log.info(
            "span",
            extra={
                "fields": {
                    "span": span.name,
                    "span_id": span.span_id,
                    "parent_id": span.parent_id,
                    "duration_ms": round(span.duration_ms, 2),
                    "status": span.status,
                    "error_type": span.error_type,
                    **{f"attr_{k}": v for k, v in span.attributes.items()},
                }
            },
        )


class InMemoryTracer:
    """Keeps finished spans, for tests and tooling."""

    def __init__(self) -> None:
        self.spans: list[SpanRecord] = []

    def on_end(self, span: SpanRecord) -> None:
        self.spans.append(span)

    def trace(self, trace_id: str) -> list[SpanRecord]:
        return [s for s in self.spans if s.trace_id == trace_id]
