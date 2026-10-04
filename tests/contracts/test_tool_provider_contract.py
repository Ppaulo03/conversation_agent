"""ToolProvider contract: FakeToolProvider and HTTPToolProvider pass the same suite."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from conftest import ApiHandle
from conversation_agent.adapters.tools.fake import FakeToolProvider
from conversation_agent.adapters.tools.http import HTTPToolProvider, local_dev_connection
from conversation_agent.core.definitions.binding import ResolvedToolBinding
from conversation_agent.core.models.tooling import ToolContext, ToolError, ToolResult
from conversation_agent.ports.tool_provider import ToolProvider
from support.server import unused_port
from vertical_slice.definitions import CONNECTION, build_agent

CONTEXT = ToolContext(
    tenant_id="t1",
    agent_id="a1",
    agent_version="1",
    channel_id="cli",
    conversation_id="c1",
    session_id="s1",
    contact_id="p1",
    turn_id="turn1",
    invocation_id="inv1",
    trace_id="trace1",
)

CANONICAL_STATUSES = {
    "success", "validation_error", "business_error", "policy_denied",
    "technical_error", "timeout", "unknown",
}  # fmt: skip


def availability_binding() -> ResolvedToolBinding:
    resolved = build_agent().resolve("scheduling.availability")
    assert resolved is not None
    return resolved


def tool_args(service_code: str = "HC-01") -> dict[str, Any]:
    return {
        "service_code": service_code,
        "start": "2026-10-06",
        "end": "2026-10-06",
        "cursor": None,
        "limit": 20,
    }


@dataclass
class Case:
    provider: ToolProvider
    args: dict[str, Any]


class FakeHarness:
    def success(self) -> Case:
        data = {"items": [], "pagination": {"next_cursor": None}}
        provider = FakeToolProvider(
            {"erp_get_available_slots": ToolResult(status="success", data=data)}
        )
        return Case(provider, tool_args())

    def business_failure(self) -> Case:
        failure = ToolResult(
            status="business_error", error=ToolError(code="X", message_safe="refused")
        )
        return Case(FakeToolProvider({"erp_get_available_slots": failure}), tool_args("ZZ"))

    def unreachable(self) -> Case:
        failure = ToolResult(
            status="technical_error", error=ToolError(code="DOWN", message_safe="down")
        )
        return Case(FakeToolProvider({"erp_get_available_slots": failure}), tool_args())


class HttpHarness:
    def __init__(self, api: ApiHandle) -> None:
        self._api = api

    def _provider(self, base_url: str) -> HTTPToolProvider:
        return HTTPToolProvider.static({CONNECTION: local_dev_connection(base_url)})

    def success(self) -> Case:
        return Case(self._provider(self._api.base_url), tool_args())

    def business_failure(self) -> Case:
        return Case(self._provider(self._api.base_url), tool_args("ZZ"))  # 404 -> business_error

    def unreachable(self) -> Case:
        return Case(self._provider(f"http://127.0.0.1:{unused_port()}"), tool_args())


@pytest.fixture(params=["fake", "http"])
async def harness(request: pytest.FixtureRequest, api: ApiHandle) -> AsyncIterator[Any]:
    harness = FakeHarness() if request.param == "fake" else HttpHarness(api)
    yield harness


def assert_failure_is_canonical(result: ToolResult) -> None:
    assert isinstance(result, ToolResult)
    assert result.status in CANONICAL_STATUSES
    assert result.status != "success"
    assert result.error is not None
    assert result.error.code
    assert result.error.message_safe


async def test_success_returns_canonical_data(harness: Any) -> None:
    case = harness.success()
    result = await case.provider.execute(availability_binding(), case.args, CONTEXT)
    assert isinstance(result, ToolResult)
    assert result.status == "success"
    assert result.error is None
    assert isinstance(result.data, dict | list)


async def test_business_failure_is_a_canonical_result_not_an_exception(harness: Any) -> None:
    case = harness.business_failure()
    result = await case.provider.execute(availability_binding(), case.args, CONTEXT)
    assert_failure_is_canonical(result)
    assert result.status == "business_error"


async def test_unreachable_system_is_a_canonical_result_not_an_exception(harness: Any) -> None:
    case = harness.unreachable()
    result = await case.provider.execute(availability_binding(), case.args, CONTEXT)
    assert_failure_is_canonical(result)
    assert result.status in {"technical_error", "timeout", "unknown"}
