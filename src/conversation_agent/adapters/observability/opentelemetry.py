"""Optional OpenTelemetry/OTLP adapter for the framework's dependency-free spans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
    from opentelemetry import context as otel_context
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace import Status, StatusCode
except ModuleNotFoundError as exc:  # pragma: no cover - exercised in a subprocess
    raise ImportError(
        'the OpenTelemetry adapter needs its SDK: pip install "conversation-agent[otel]"'
    ) from exc

from conversation_agent.core.tracing import SpanRecord, SpanStart


class OpenTelemetryTracer:
    """Mirrors each framework span into an OpenTelemetry tracer.

    The framework trace/span ids remain attributes for correlation with logs and durable rows; the
    OpenTelemetry SDK owns its wire ids and parent context.
    """

    def __init__(self, tracer: trace.Tracer) -> None:
        self._tracer = tracer
        self._active: dict[str, tuple[trace.Span, Any]] = {}

    def on_start(self, span: SpanStart) -> None:
        attributes = {
            "conversation_agent.trace_id": span.trace_id,
            "conversation_agent.span_id": span.span_id,
            **{key: value for key, value in span.attributes.items() if value is not None},
        }
        started = self._tracer.start_span(
            span.name,
            attributes=attributes,
            start_time=int(span.started_at.timestamp() * 1_000_000_000),
        )
        token = otel_context.attach(trace.set_span_in_context(started))
        self._active[span.span_id] = (started, token)

    def on_end(self, span: SpanRecord) -> None:
        active = self._active.pop(span.span_id, None)
        if active is None:
            return
        exported, token = active
        try:
            if span.status == "error":
                exported.set_status(Status(StatusCode.ERROR, span.error_type or "error"))
                if span.error_type is not None:
                    exported.set_attribute("error.type", span.error_type)
            exported.end(
                end_time=int(
                    span.started_at.timestamp() * 1_000_000_000 + span.duration_ms * 1_000_000
                )
            )
        finally:
            otel_context.detach(token)


@dataclass(frozen=True)
class OTLPTracing:
    tracer: OpenTelemetryTracer
    provider: TracerProvider

    def shutdown(self) -> None:
        self.provider.force_flush()
        self.provider.shutdown()


def build_otlp_tracing(*, service_name: str = "conversation-agent") -> OTLPTracing:
    """Build the official OTLP/HTTP pipeline.

    The exporter reads standard ``OTEL_EXPORTER_OTLP_*`` environment variables, including endpoint,
    headers, certificate and timeout. A local provider avoids mutating OpenTelemetry's global state.
    """
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    return OTLPTracing(
        tracer=OpenTelemetryTracer(provider.get_tracer("conversation_agent")), provider=provider
    )
