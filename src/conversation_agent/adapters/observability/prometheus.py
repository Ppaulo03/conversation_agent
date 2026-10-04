"""Prometheus text exposition, with no client library: the format is small and stable.

`GAUGES` is the ONE list of health metrics: the exporter renders from it, and the SLO artifacts
(`slo`) are validated against it, so an alert can never point at a metric that does not exist.
"""

from __future__ import annotations

from conversation_agent.adapters.llm.metrics import Histogram, InMemoryLLMMetrics
from conversation_agent.adapters.observability.runtime_metrics import InMemoryRuntimeMetrics
from conversation_agent.adapters.postgres.health import HealthSnapshot
from conversation_agent.adapters.tools.metrics import InMemoryToolMetrics

PREFIX = "conversation_agent_"

GAUGES: dict[str, tuple[str, str]] = {
    # metric name (without prefix) -> (HealthSnapshot attribute, help text)
    "inbox_ready": ("inbox_ready", "Inbound events waiting for a turn."),
    "inbox_oldest_ready_age_seconds": (
        "inbox_oldest_ready_age_seconds",
        "Age of the oldest inbound event still waiting for a turn.",
    ),
    "turns_processing": ("turns_processing", "Turns currently open."),
    "turns_oldest_processing_age_seconds": (
        "turns_oldest_processing_age_seconds",
        "Age of the oldest open turn (a stuck turn grows without bound).",
    ),
    "outbox_pending": ("outbox_pending", "Replies due to be sent and not yet sent."),
    "outbox_oldest_pending_age_seconds": (
        "outbox_oldest_pending_age_seconds",
        "How long the oldest due reply has waited to be sent.",
    ),
    "outbox_unsettled": (
        "outbox_unsettled",
        "Replies sent whose delivery is not confirmed (queued, unknown, reconciling).",
    ),
    "outbox_oldest_unsettled_age_seconds": (
        "outbox_oldest_unsettled_age_seconds",
        "Age of the oldest unconfirmed reply (compare with the gateway idempotency window).",
    ),
    "invocations_unresolved": (
        "invocations_unresolved",
        "External effects whose outcome is not known yet.",
    ),
    "invocations_oldest_unresolved_age_seconds": (
        "invocations_oldest_unresolved_age_seconds",
        "Age of the oldest external effect with an unknown outcome.",
    ),
    "invocations_human_handoff": (
        "invocations_human_handoff",
        "External effects waiting for a person to decide.",
    ),
    "scheduled_overdue": ("scheduled_overdue", "Timers past their due time and not done."),
    "scheduled_oldest_overdue_age_seconds": (
        "scheduled_oldest_overdue_age_seconds",
        "How long the most overdue timer has been overdue.",
    ),
    "handoffs_pending": ("handoffs_pending", "Conversations waiting for a person to take them."),
    "handoffs_oldest_pending_age_seconds": (
        "handoffs_oldest_pending_age_seconds",
        "How long the oldest handoff has waited for a person.",
    ),
    "pending_confirmations": (
        "pending_confirmations",
        "Protected actions waiting for the contact's confirmation.",
    ),
}

TOOL_COUNTER = "tool_calls_total"
TOOL_DURATION = "tool_call_duration_seconds"
CIRCUIT_COUNTER = "circuit_transitions_total"
COUNTERS = {TOOL_COUNTER, f"{TOOL_DURATION}_count", f"{TOOL_DURATION}_sum", CIRCUIT_COUNTER}
LLM_CALLS = "llm_calls_total"
LLM_TOKENS = "llm_tokens_total"
LLM_COST = "llm_cost_usd_total"
LLM_UNPRICED = "llm_unpriced_calls_total"
LLM_DURATION = "llm_call_duration_seconds"
LLM_WRITE_FAILURES = "llm_usage_write_failures_total"
TURNS = "turns_total"
TURN_PROCESSING = "turn_processing_seconds"
TURN_QUEUE = "turn_queue_wait_seconds"
TURN_LLM_CALLS = "turn_llm_calls_total"
TURN_PROPOSALS = "turn_proposals_total"
HANDOFFS = "handoffs_total"
RUNTIME_METRICS = {
    TURNS,
    f"{TURN_PROCESSING}_bucket",
    f"{TURN_PROCESSING}_sum",
    f"{TURN_PROCESSING}_count",
    f"{TURN_QUEUE}_bucket",
    f"{TURN_QUEUE}_sum",
    f"{TURN_QUEUE}_count",
    TURN_LLM_CALLS,
    TURN_PROPOSALS,
    HANDOFFS,
}
LLM_METRICS = {
    LLM_CALLS,
    LLM_TOKENS,
    LLM_COST,
    LLM_UNPRICED,
    f"{LLM_DURATION}_bucket",
    f"{LLM_DURATION}_sum",
    f"{LLM_DURATION}_count",
    LLM_WRITE_FAILURES,
}


def _label(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**labels: object) -> str:
    return "{" + ",".join(f'{k}="{_label(v)}"' for k, v in labels.items()) + "}"


def available_metrics() -> set[str]:
    """Every metric name the exporter can emit (full names, no suffix games)."""
    names = {PREFIX + g for g in GAUGES}
    names |= {PREFIX + c for c in COUNTERS} | {PREFIX + f"{TOOL_DURATION}_max"}
    names |= {PREFIX + m for m in LLM_METRICS} | {PREFIX + m for m in RUNTIME_METRICS}
    return names


def _histogram_lines(
    name: str, help_text: str, series: dict[tuple[str, str], Histogram]
) -> list[str]:
    lines = [f"# HELP {PREFIX}{name} {help_text}", f"# TYPE {PREFIX}{name} histogram"]
    for (agent_id, version), hist in sorted(series.items()):
        base = {"agent_id": agent_id, "agent_version": version}
        for bound, count in zip((*hist.buckets, float("inf")), hist.counts, strict=True):
            le = "+Inf" if bound == float("inf") else f"{bound:g}"
            lines.append(f"{PREFIX}{name}_bucket{_labels(**base, le=le)} {count}")
        lines.append(f"{PREFIX}{name}_sum{_labels(**base)} {hist.total:g}")
        lines.append(f"{PREFIX}{name}_count{_labels(**base)} {hist.count}")
    return lines


def _render_runtime(rt: InMemoryRuntimeMetrics) -> list[str]:
    lines = [
        f"# HELP {PREFIX}{TURNS} Turns by outcome (completed, failed, retry, waiting, silent).",
        f"# TYPE {PREFIX}{TURNS} counter",
    ]
    for (agent_id, version, outcome), count in sorted(rt.turns.items()):
        labels = _labels(agent_id=agent_id, agent_version=version, outcome=outcome)
        lines.append(f"{PREFIX}{TURNS}{labels} {count}")
    lines += _histogram_lines(
        TURN_PROCESSING, "Time spent processing a turn (lease to commit).", rt.processing
    )
    lines += _histogram_lines(
        TURN_QUEUE,
        "How long the oldest message of a turn waited before processing began.",
        rt.queue_wait,
    )
    for name, help_text, counter in (
        (TURN_LLM_CALLS, "LLM calls made by turns.", rt.llm_calls),
        (TURN_PROPOSALS, "Protected actions proposed by turns.", rt.proposals),
        (HANDOFFS, "Conversations handed to a person by a turn.", rt.handoffs),
    ):
        lines += [f"# HELP {PREFIX}{name} {help_text}", f"# TYPE {PREFIX}{name} counter"]
        for (agent_id, version), count in sorted(counter.items()):
            lines.append(
                f"{PREFIX}{name}{_labels(agent_id=agent_id, agent_version=version)} {count}"
            )
    return lines


def _render_llm(llm: InMemoryLLMMetrics) -> list[str]:
    lines = [
        f"# HELP {PREFIX}{LLM_CALLS} LLM calls by outcome.",
        f"# TYPE {PREFIX}{LLM_CALLS} counter",
    ]
    for (provider, model, purpose, outcome, code), count in sorted(llm.calls.items()):
        labels = _labels(
            provider=provider, model=model, purpose=purpose, outcome=outcome, error_code=code
        )
        lines.append(f"{PREFIX}{LLM_CALLS}{labels} {count}")
    lines += [
        f"# HELP {PREFIX}{LLM_TOKENS} LLM tokens by kind (input, output, cache_read, cache_write).",
        f"# TYPE {PREFIX}{LLM_TOKENS} counter",
    ]
    for (provider, model, purpose, kind), amount in sorted(llm.tokens.items()):
        labels = _labels(provider=provider, model=model, purpose=purpose, kind=kind)
        lines.append(f"{PREFIX}{LLM_TOKENS}{labels} {amount}")
    lines += [
        f"# HELP {PREFIX}{LLM_COST} Estimated LLM cost in USD (priced calls only).",
        f"# TYPE {PREFIX}{LLM_COST} counter",
    ]
    for (provider, model, purpose), cost in sorted(llm.cost_usd.items()):
        labels = _labels(provider=provider, model=model, purpose=purpose)
        lines.append(f"{PREFIX}{LLM_COST}{labels} {cost:.8f}")
    lines += [
        f"# HELP {PREFIX}{LLM_UNPRICED} LLM calls whose model has no price (cost unknown).",
        f"# TYPE {PREFIX}{LLM_UNPRICED} counter",
    ]
    for (provider, model), count in sorted(llm.unpriced.items()):
        lines.append(f"{PREFIX}{LLM_UNPRICED}{_labels(provider=provider, model=model)} {count}")
    lines += [
        f"# HELP {PREFIX}{LLM_DURATION} LLM call duration.",
        f"# TYPE {PREFIX}{LLM_DURATION} histogram",
    ]
    for (provider, model, purpose), hist in sorted(llm.latency.items()):
        base = {"provider": provider, "model": model, "purpose": purpose}
        for bound, count in zip((*hist.buckets, float("inf")), hist.counts, strict=True):
            le = "+Inf" if bound == float("inf") else f"{bound:g}"
            lines.append(f"{PREFIX}{LLM_DURATION}_bucket{_labels(**base, le=le)} {count}")
        lines.append(f"{PREFIX}{LLM_DURATION}_sum{_labels(**base)} {hist.total:g}")
        lines.append(f"{PREFIX}{LLM_DURATION}_count{_labels(**base)} {hist.count}")
    lines += [
        f"# HELP {PREFIX}{LLM_WRITE_FAILURES} Usage records that could not be written.",
        f"# TYPE {PREFIX}{LLM_WRITE_FAILURES} counter",
        f"{PREFIX}{LLM_WRITE_FAILURES} {llm.usage_write_failures}",
    ]
    return lines


def render(
    snapshot: HealthSnapshot,
    tools: InMemoryToolMetrics | None = None,
    llm: InMemoryLLMMetrics | None = None,
    runtime: InMemoryRuntimeMetrics | None = None,
) -> str:
    lines: list[str] = []
    values = snapshot.as_dict()
    for name, (attribute, help_text) in GAUGES.items():
        lines += [
            f"# HELP {PREFIX}{name} {help_text}",
            f"# TYPE {PREFIX}{name} gauge",
            f"{PREFIX}{name} {values[attribute]:g}",
        ]
    if tools is not None:
        lines += [
            f"# HELP {PREFIX}{TOOL_COUNTER} Outbound tool calls by canonical outcome.",
            f"# TYPE {PREFIX}{TOOL_COUNTER} counter",
        ]
        for (provider, tool, status, code), count in sorted(
            tools.calls.items(), key=lambda kv: tuple(str(p) for p in kv[0])
        ):
            labels = _labels(provider=provider, tool=tool, status=status, error_code=code or "")
            lines.append(f"{PREFIX}{TOOL_COUNTER}{labels} {count}")
        for suffix, kind, attribute in (
            ("count", "counter", "count"),
            ("sum", "counter", "total_ms"),
            ("max", "gauge", "max_ms"),
        ):
            lines += [
                f"# HELP {PREFIX}{TOOL_DURATION}_{suffix} Outbound tool call duration ({suffix}).",
                f"# TYPE {PREFIX}{TOOL_DURATION}_{suffix} {kind}",
            ]
            for (provider, tool), summary in sorted(tools.latency.items()):
                raw = getattr(summary, attribute)
                value = raw if attribute == "count" else raw / 1000.0
                labels = _labels(provider=provider, tool=tool)
                lines.append(f"{PREFIX}{TOOL_DURATION}_{suffix}{labels} {value:g}")
        lines += [
            f"# HELP {PREFIX}{CIRCUIT_COUNTER} Circuit breaker state changes.",
            f"# TYPE {PREFIX}{CIRCUIT_COUNTER} counter",
        ]
        transitions: dict[tuple[str, str, str], int] = {}
        for entry in tools.circuits:
            transitions[entry] = transitions.get(entry, 0) + 1
        for (provider, scope, state), count in sorted(transitions.items()):
            labels = _labels(provider=provider, scope=scope, state=state)
            lines.append(f"{PREFIX}{CIRCUIT_COUNTER}{labels} {count}")
    if runtime is not None:
        lines += _render_runtime(runtime)
    if llm is not None:
        lines += _render_llm(llm)
    return "\n".join(lines) + "\n"
