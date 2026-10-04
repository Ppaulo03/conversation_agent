"""Compiler-style rules enforced at definition time, and the retry semantics they protect
(INV-006, INV-021, DESIGN §5.2): bindings never lower protection; writes retry only with
idempotency and the same key; unknown is never retried."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from conversation_agent.core.definitions.agent import AgentDefinition
from conversation_agent.core.definitions.tool import RetryPolicy
from conversation_agent.core.errors import DefinitionError
from conversation_agent.core.models.tooling import ToolContext, ToolError, ToolResult
from conversation_agent.engine.tool_runner import ToolRunner
from conversation_agent.tools.requests import build_capability_request
from vertical_slice.definitions import (
    CREATE,
    CREATE_BINDING,
    ERP_CREATE_RESERVATION,
    ERP_GET_AVAILABLE_SLOTS,
    build_agent,
)

CTX = ToolContext(
    tenant_id="t", agent_id="a", agent_version="1", channel_id="c", conversation_id="cv",
    session_id="s", contact_id="p", turn_id="turn", invocation_id="inv", trace_id="tr",
)  # fmt: skip


def with_retry(tool: Any, **updates: Any) -> Any:
    return tool.model_validate({**tool.model_dump(), **updates})


# --- definition-time rules ---


def test_a_write_cannot_retry_without_idempotency_support() -> None:  # INV-021
    with pytest.raises(ValidationError, match="idempotency"):
        with_retry(
            ERP_CREATE_RESERVATION,
            idempotency_supported=False,
            recovery=None,
            retry=RetryPolicy(max_attempts=2, mode="same_idempotency_key"),
        )


def test_a_write_cannot_retry_with_a_fresh_key() -> None:  # INV-021
    with pytest.raises(ValidationError, match="SAME"):
        with_retry(ERP_CREATE_RESERVATION, retry=RetryPolicy(max_attempts=2, mode="none"))


def test_a_write_with_idempotency_and_the_same_key_may_retry() -> None:
    ok = with_retry(
        ERP_CREATE_RESERVATION, retry=RetryPolicy(max_attempts=3, mode="same_idempotency_key")
    )
    assert ok.retry.max_attempts == 3


def test_reads_may_retry_freely_and_attempts_must_be_positive() -> None:
    ok = with_retry(ERP_GET_AVAILABLE_SLOTS, retry=RetryPolicy(max_attempts=3))
    assert ok.retry.max_attempts == 3
    with pytest.raises(ValidationError):
        with_retry(ERP_GET_AVAILABLE_SLOTS, retry=RetryPolicy(max_attempts=0))


def test_a_binding_can_raise_protection_but_never_lower_it() -> None:
    agent = build_agent()
    lowered = CREATE_BINDING.model_copy(update={"risk": "read"})  # capability+tool: irreversible
    fields = {k: getattr(agent, k) for k in AgentDefinition.model_fields}
    fields["bindings"] = tuple(
        lowered if b.capability == CREATE.name else b for b in agent.bindings
    )
    with pytest.raises(DefinitionError, match="never lower"):
        AgentDefinition.model_validate(fields)


# --- retry semantics ---


class Scripted:
    def __init__(self, *results: ToolResult) -> None:
        self._results = list(results)
        self.calls = 0

    async def execute(self, *_: Any, **__: Any) -> ToolResult:
        self.calls += 1
        return self._results.pop(0)


def fail(status: Any, retryable: bool) -> ToolResult:
    return ToolResult(
        status=status, error=ToolError(code="X", message_safe="x", retryable=retryable)
    )


OK_READ = ToolResult(status="success", data={"items": [], "pagination": {"next_cursor": None}})
OK_WRITE = ToolResult(status="success", data={"id": "b1", "state": "confirmed"})
READ_ARGS = {"service_id": "haircut", "from_date": "2026-10-06", "to_date": "2026-10-06"}
WRITE_ARGS = {
    "service_id": "haircut",
    "start_at": "2026-10-06T10:00:00-03:00",
    "duration_minutes": 30,
}


async def run(tool: Any, capability: str, args: dict[str, Any], provider: Scripted) -> Any:
    agent = build_agent()
    agent = agent.model_copy(
        update={"tools": tuple(tool if t.name == tool.name else t for t in agent.tools)}
    )
    resolved = agent.resolve(capability)
    assert resolved is not None
    request = build_capability_request(resolved.capability, args)
    return await ToolRunner({"http": provider}).run(resolved, request, CTX)


async def test_a_read_retries_technical_failures_up_to_max_attempts() -> None:
    tool = with_retry(ERP_GET_AVAILABLE_SLOTS, retry=RetryPolicy(max_attempts=3))
    flaky = Scripted(fail("technical_error", True), fail("timeout", True), OK_READ)
    result = await run(tool, "scheduling.availability", READ_ARGS, flaky)
    assert result.status == "success" and flaky.calls == 3


async def test_a_read_gives_up_after_max_attempts_and_skips_non_retryable_errors() -> None:
    tool = with_retry(ERP_GET_AVAILABLE_SLOTS, retry=RetryPolicy(max_attempts=2))
    down = Scripted(fail("technical_error", True), fail("technical_error", True), OK_READ)
    assert (await run(tool, "scheduling.availability", READ_ARGS, down)).status == "technical_error"
    assert down.calls == 2
    business = Scripted(fail("business_error", False), OK_READ)
    result = await run(tool, "scheduling.availability", READ_ARGS, business)
    assert result.status == "business_error" and business.calls == 1


async def test_unknown_is_never_retried_even_when_the_tool_allows_retries() -> None:  # INV-006
    tool = with_retry(
        ERP_CREATE_RESERVATION, retry=RetryPolicy(max_attempts=5, mode="same_idempotency_key")
    )
    ambiguous = Scripted(fail("unknown", False), OK_WRITE)
    result = await run(tool, "scheduling.create", WRITE_ARGS, ambiguous)
    assert result.status == "unknown" and ambiguous.calls == 1


async def test_a_write_retries_only_a_failure_that_provably_never_reached_the_system() -> None:
    tool = with_retry(
        ERP_CREATE_RESERVATION, retry=RetryPolicy(max_attempts=3, mode="same_idempotency_key")
    )
    refused = Scripted(fail("technical_error", True), OK_WRITE)  # e.g. connection refused
    assert (await run(tool, "scheduling.create", WRITE_ARGS, refused)).status == "success"
    assert refused.calls == 2
    timeout = Scripted(fail("timeout", True), OK_WRITE)  # may have been delivered: not retried
    assert (await run(tool, "scheduling.create", WRITE_ARGS, timeout)).status == "timeout"
    assert timeout.calls == 1


async def test_a_write_without_a_retry_policy_is_never_retried() -> None:
    refused = Scripted(fail("technical_error", True), OK_WRITE)
    result = await run(ERP_CREATE_RESERVATION, "scheduling.create", WRITE_ARGS, refused)
    assert result.status == "technical_error" and refused.calls == 1
