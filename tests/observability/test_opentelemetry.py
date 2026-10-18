from __future__ import annotations

import io
import logging

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from conversation_agent.adapters.observability.opentelemetry import OpenTelemetryTracer
from conversation_agent.app.observability import setup_observability
from conversation_agent.core.observability import bind
from conversation_agent.core.tracing import configure_tracer, span


def test_framework_spans_keep_their_hierarchy_and_correlation_in_opentelemetry() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    configure_tracer(OpenTelemetryTracer(provider.get_tracer("test")))
    try:
        with bind(trace_id="internal-trace"), span("outer", step=1):
            with span("inner"):
                pass
            with pytest.raises(ValueError), span("failed"):
                raise ValueError("content that must not be exported")
    finally:
        configure_tracer(None)
        provider.shutdown()

    spans = {item.name: item for item in exporter.get_finished_spans()}
    outer, inner, failed = spans["outer"], spans["inner"], spans["failed"]
    assert inner.parent is not None and inner.parent.span_id == outer.context.span_id
    assert failed.parent is not None and failed.parent.span_id == outer.context.span_id
    assert len({item.context.trace_id for item in spans.values()}) == 1
    assert outer.attributes["conversation_agent.trace_id"] == "internal-trace"
    assert outer.attributes["step"] == 1
    assert failed.status.status_code is StatusCode.ERROR
    assert failed.attributes["error.type"] == "ValueError"
    assert "content that must not be exported" not in repr(failed.attributes)


def test_the_observability_kit_can_select_and_shutdown_the_optional_otlp_pipeline() -> None:
    obs = setup_observability(
        environ={
            "TRACE": "otlp",
            "OTEL_SERVICE_NAME": "conversation-agent-test",
            "LLM_PRICES_FILE": "/no/such/file.yaml",
        },
        stream=io.StringIO(),
    )
    try:
        assert obs.prices.models == {}
    finally:
        obs.shutdown()
        logger = logging.getLogger("conversation_agent")
        for handler in list(logger.handlers):
            if getattr(handler, "_conversation_agent_json", False):
                logger.removeHandler(handler)
        logger.propagate = True
