"""Capability <-> Tool binding through the ToolRunner (input/output/list/error mapping)."""

from __future__ import annotations

from typing import Any

import pytest

from conversation_agent.adapters.tools.fake import FakeToolProvider
from conversation_agent.core.definitions.binding import ResolvedToolBinding
from conversation_agent.core.models.tooling import ToolError, ToolResult
from conversation_agent.engine.tool_runner import ToolRunner
from conversation_agent.tools.requests import RequestRejected, build_capability_request
from vertical_slice.definitions import AVAILABILITY, CREATE, build_agent

from .test_tool_provider_contract import CONTEXT

ERP_PAGE: dict[str, Any] = {
    "items": [
        {"start": "2026-10-06T09:00:00-03:00", "end": "2026-10-06T09:30:00-03:00"},
        {"start": "2026-10-06T09:30:00-03:00", "end": "2026-10-06T10:00:00-03:00"},
    ],
    "pagination": {"next_cursor": "2"},
}


def resolved(name: str) -> ResolvedToolBinding:
    r = build_agent().resolve(name)
    assert r is not None
    return r


def runner_with(tool: str, result: ToolResult) -> tuple[ToolRunner, FakeToolProvider]:
    provider = FakeToolProvider({tool: result})
    return ToolRunner({"http": provider}), provider


async def test_input_mapping_translates_capability_args_to_tool_args() -> None:
    runner, provider = runner_with(
        "erp_get_available_slots", ToolResult(status="success", data=ERP_PAGE)
    )
    request = build_capability_request(
        AVAILABILITY,
        {"service_id": "consultation", "from_date": "2026-10-06", "to_date": "2026-10-07"},
    )
    await runner.run(resolved("scheduling.availability"), request, CONTEXT)
    assert provider.calls[0].args == {
        "service_code": "CN-01",  # enum mapping
        "start": "2026-10-06",  # rename
        "end": "2026-10-07",
        "cursor": None,
        "limit": 20,  # constant
    }


async def test_output_and_list_mapping_present_the_capability_schema() -> None:
    runner, _ = runner_with("erp_get_available_slots", ToolResult(status="success", data=ERP_PAGE))
    request = build_capability_request(
        AVAILABILITY, {"service_id": "haircut", "from_date": "2026-10-06", "to_date": "2026-10-06"}
    )
    result = await runner.run(resolved("scheduling.availability"), request, CONTEXT)
    assert result.status == "success"
    assert result.data == {
        "slots": [
            {"start_at": "2026-10-06T09:00:00-03:00", "end_at": "2026-10-06T09:30:00-03:00"},
            {"start_at": "2026-10-06T09:30:00-03:00", "end_at": "2026-10-06T10:00:00-03:00"},
        ],
        "next_cursor": "2",
    }
    assert "items" not in (result.data or {})  # API schema never leaks into the capability view


async def test_unit_conversion_and_utc_normalisation_on_create_binding() -> None:
    runner, provider = runner_with(
        "erp_create_reservation", ToolResult(status="success", data={"id": "B1", "state": "ok"})
    )
    request = build_capability_request(
        CREATE,
        {"service_id": "haircut", "start_at": "2026-10-06T10:00:00-03:00", "duration_minutes": 30},
    )
    result = await runner.run(resolved("scheduling.create"), request, CONTEXT)
    assert provider.calls[0].args == {
        "service_code": "HC-01",
        "starts_at": "2026-10-06T13:00:00+00:00",
        "hours": 0.5,
    }
    assert result.data == {"booking_id": "B1", "status": "ok"}


async def test_provider_errors_pass_through_as_canonical_results() -> None:
    failure = ToolResult(
        status="business_error", error=ToolError(code="SLOT_UNAVAILABLE", message_safe="taken")
    )
    runner, _ = runner_with("erp_get_available_slots", failure)
    request = build_capability_request(
        AVAILABILITY, {"service_id": "haircut", "from_date": "2026-10-06", "to_date": "2026-10-06"}
    )
    result = await runner.run(resolved("scheduling.availability"), request, CONTEXT)
    assert result.status == "business_error"
    assert result.error is not None and result.error.code == "SLOT_UNAVAILABLE"
    assert result.data is None


@pytest.mark.parametrize("name", ["scheduling.availability", "scheduling.create"])
async def test_malformed_success_body_is_not_reported_as_success(name: str) -> None:
    tool = resolved(name).tool.name
    runner, _ = runner_with(tool, ToolResult(status="success", data={"unexpected": True}))
    if name == "scheduling.availability":
        request = build_capability_request(
            AVAILABILITY,
            {"service_id": "haircut", "from_date": "2026-10-06", "to_date": "2026-10-06"},
        )
        expected = "technical_error"  # read
    else:
        request = build_capability_request(
            CREATE,
            {
                "service_id": "haircut",
                "start_at": "2026-10-06T10:00:00-03:00",
                "duration_minutes": 30,
            },
        )
        expected = "unknown"  # a write may have happened
    result = await runner.run(resolved(name), request, CONTEXT)
    assert result.status == expected
    assert result.data is None


async def test_provider_raising_is_contained_and_conservative() -> None:
    class Exploding:
        async def execute(self, *_: Any, **__: Any) -> ToolResult:
            raise RuntimeError("secret internal detail")

    runner = ToolRunner({"http": Exploding()})
    request = build_capability_request(
        CREATE,
        {"service_id": "haircut", "start_at": "2026-10-06T10:00:00-03:00", "duration_minutes": 30},
    )
    result = await runner.run(resolved("scheduling.create"), request, CONTEXT)
    assert result.status == "unknown"
    assert "secret internal detail" not in result.model_dump_json()


async def test_missing_provider_is_a_technical_error() -> None:
    request = build_capability_request(
        AVAILABILITY, {"service_id": "haircut", "from_date": "2026-10-06", "to_date": "2026-10-06"}
    )
    result = await ToolRunner({}).run(resolved("scheduling.availability"), request, CONTEXT)
    assert result.status == "technical_error"


def test_capability_request_is_validated_against_capability_schema() -> None:
    with pytest.raises(RequestRejected):
        build_capability_request(AVAILABILITY, {"service_id": "massage", "from_date": "x"})
    with pytest.raises(RequestRejected):  # naive datetimes are ambiguous -> rejected
        build_capability_request(
            CREATE,
            {"service_id": "haircut", "start_at": "2026-10-06T10:00:00", "duration_minutes": 30},
        )


def test_capability_request_hash_is_canonical_and_timezone_stable() -> None:
    a = build_capability_request(
        CREATE,
        {"service_id": "haircut", "start_at": "2026-10-06T10:00:00-03:00", "duration_minutes": 30},
    )
    b = build_capability_request(
        CREATE,
        {"duration_minutes": 30, "start_at": "2026-10-06T13:00:00+00:00", "service_id": "haircut"},
    )
    c = build_capability_request(
        CREATE,
        {"service_id": "haircut", "start_at": "2026-10-06T11:00:00-03:00", "duration_minutes": 30},
    )
    assert a.args_hash == b.args_hash  # same instant, any key order / offset
    assert a.args_hash != c.args_hash  # any protected arg change changes the hash


def test_effective_risk_never_lowers_protection() -> None:
    r = resolved("scheduling.availability")
    assert r.effective_risk == "read" and not r.effective_confirmation_required
    # A binding declaring a lower risk than its capability/tool cannot reduce protection.
    create = resolved("scheduling.create")
    downgraded = create.model_copy(
        update={"binding": create.binding.model_copy(update={"risk": "read"})}
    )
    assert downgraded.effective_risk == "irreversible"
    assert downgraded.effective_confirmation_required
    # And a binding can raise it.
    raised = r.model_copy(update={"binding": r.binding.model_copy(update={"risk": "write"})})
    assert raised.effective_risk == "write"
