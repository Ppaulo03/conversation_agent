"""Generic ToolProvider side-effect safety contract (ADR-024, INV-006, INV-021).

The contract is exercised through a transport-free provider, so a custom provider's obligations
can be read without HTTP/MCP internals; the first-party adapters then show the identity surviving
a real retry. Compiler rules and the single-send header/key tests live next to their subjects
(`test_retry_and_risk_rules.py`, `test_http_tool_provider.py`, `test_mcp_provider.py`)."""

from __future__ import annotations

from typing import Any

import httpx

from conftest import McpHandle
from conversation_agent.adapters.tools.http import HTTPToolProvider, local_dev_connection
from conversation_agent.core.definitions.tool import RetryPolicy
from conversation_agent.core.models.tooling import ToolContext, ToolError, ToolResult
from conversation_agent.engine.tool_runner import ToolRunner
from conversation_agent.tools.requests import build_capability_request
from vertical_slice.definitions import (
    CONNECTION,
    ERP_CREATE_RESERVATION,
    ERP_GET_AVAILABLE_SLOTS,
    build_agent,
)

from .test_mcp_provider import TICKET, binding_for, provider_for
from .test_retry_and_risk_rules import (
    CTX,
    OK_READ,
    OK_WRITE,
    READ_ARGS,
    WRITE_ARGS,
    fail,
    with_retry,
)

RETRY_SAME_KEY = RetryPolicy(max_attempts=3, mode="same_idempotency_key")


class RecordingProvider:
    """What any ToolProvider is: a scripted classification plus the identity it was handed."""

    def __init__(self, *outcomes: ToolResult | Exception) -> None:
        self._outcomes = list(outcomes)
        self.invocation_ids: list[str] = []

    async def destination_fingerprint(self, *_: Any, **__: Any) -> str | None:
        return None

    async def execute(self, binding: Any, args: Any, context: ToolContext, **_: Any) -> ToolResult:
        self.invocation_ids.append(context.invocation_id)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def run(tool: Any, capability: str, args: dict[str, Any], provider: Any) -> Any:
    agent = build_agent()
    agent = agent.model_copy(
        update={"tools": tuple(tool if t.name == tool.name else t for t in agent.tools)}
    )
    resolved = agent.resolve(capability)
    assert resolved is not None
    request = build_capability_request(resolved.capability, args)
    return await ToolRunner({"http": provider}).run(resolved, request, CTX)


# --- one logical identity across ToolRunner retries ---


async def test_a_proven_pre_send_failure_is_retried_with_the_same_invocation_identity() -> None:
    tool = with_retry(ERP_CREATE_RESERVATION, retry=RETRY_SAME_KEY)
    provider = RecordingProvider(
        fail("technical_error", True), fail("technical_error", True), OK_WRITE
    )
    result = await run(tool, "scheduling.create", WRITE_ARGS, provider)
    assert result.status == "success"
    assert provider.invocation_ids == [CTX.invocation_id] * 3  # no new identity per attempt


async def test_a_read_retry_keeps_the_invocation_identity_too() -> None:
    tool = with_retry(ERP_GET_AVAILABLE_SLOTS, retry=RetryPolicy(max_attempts=2))
    provider = RecordingProvider(fail("timeout", True), OK_READ)
    assert (await run(tool, "scheduling.availability", READ_ARGS, provider)).status == "success"
    assert provider.invocation_ids == [CTX.invocation_id] * 2


# --- ambiguity is `unknown`, and `unknown` is not an ordinary retry ---


async def test_an_ambiguous_write_is_unknown_and_never_retried_by_the_runner() -> None:
    tool = with_retry(ERP_CREATE_RESERVATION, retry=RETRY_SAME_KEY)
    ambiguous = ToolResult(
        status="unknown",
        error=ToolError(code="LOST_ANSWER", message_safe="outcome not known", retryable=False),
    )
    provider = RecordingProvider(ambiguous, OK_WRITE)
    result = await run(tool, "scheduling.create", WRITE_ARGS, provider)
    assert result.status == "unknown"
    assert provider.invocation_ids == [CTX.invocation_id]  # recovery's business, not a retry


async def test_business_refusal_of_a_write_is_final_and_not_ambiguity() -> None:
    tool = with_retry(ERP_CREATE_RESERVATION, retry=RETRY_SAME_KEY)
    provider = RecordingProvider(fail("business_error", True), OK_WRITE)
    result = await run(tool, "scheduling.create", WRITE_ARGS, provider)
    assert result.status == "business_error" and len(provider.invocation_ids) == 1


# --- provider exceptions: a contract violation, defensively classified by risk ---


async def test_an_exception_is_unknown_for_a_write_and_a_final_technical_error_for_a_read() -> None:
    write = RecordingProvider(RuntimeError("boom"), OK_WRITE)
    tool = with_retry(ERP_CREATE_RESERVATION, retry=RETRY_SAME_KEY)
    result = await run(tool, "scheduling.create", WRITE_ARGS, write)
    assert result.status == "unknown" and len(write.invocation_ids) == 1

    read = RecordingProvider(RuntimeError("boom"), OK_READ)
    tool = with_retry(ERP_GET_AVAILABLE_SLOTS, retry=RetryPolicy(max_attempts=3))
    result = await run(tool, "scheduling.availability", READ_ARGS, read)
    assert result.status == "technical_error"
    assert result.error is not None and not result.error.retryable
    assert len(read.invocation_ids) == 1  # not retried


# --- first-party HTTP: one external identity across a real technical retry ---


async def test_http_retry_of_a_write_carries_one_external_identity() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        if len(seen) == 1:
            raise httpx.ConnectError("refused", request=request)  # provably never sent
        return httpx.Response(201, json={"id": "b1", "state": "confirmed"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HTTPToolProvider.static(
        {CONNECTION: local_dev_connection("http://127.0.0.1:9")}, client=client
    )
    tool = with_retry(ERP_CREATE_RESERVATION, retry=RETRY_SAME_KEY)
    assert (await run(tool, "scheduling.create", WRITE_ARGS, provider)).status == "success"
    assert len(seen) == 2
    identities = {(h["x-invocation-id"], h["idempotency-key"]) for h in seen}
    assert identities == {(CTX.invocation_id, CTX.invocation_id)}


# --- first-party MCP: one external identity across the session-recovery resend ---


async def test_mcp_session_recovery_resends_with_the_same_external_identity(
    mcp: McpHandle,
) -> None:
    provider = provider_for(mcp)
    binding = binding_for(mcp, "create_ticket", risk="write", idempotent=True)
    context = CTX.model_copy(update={"invocation_id": "inv-mcp"})
    await provider.execute(binding, TICKET, context)
    mcp.state.sessions.clear()  # the server forgot the session: the call is re-sent
    await provider.execute(binding, TICKET, context)
    keyed = [r["headers"] for r in mcp.state.requests if "idempotency-key" in r["headers"]]
    assert len(keyed) >= 2
    assert {h["idempotency-key"] for h in keyed} == {"inv-mcp"}
    assert len(mcp.state.tickets) == 1
