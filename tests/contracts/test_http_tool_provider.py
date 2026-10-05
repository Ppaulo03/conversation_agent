"""HTTPToolProvider specifics: trusted context, error classification, URL safety."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from conftest import ApiHandle
from conversation_agent.adapters.tools.http import HTTPToolProvider, local_dev_connection
from conversation_agent.core.definitions.binding import ResolvedToolBinding
from conversation_agent.core.definitions.tool import HTTPRequestSpec
from conversation_agent.core.models.tooling import ToolResult
from support.server import unused_port
from vertical_slice.definitions import CONNECTION, build_agent

from .test_tool_provider_contract import CONTEXT, availability_binding, tool_args


def provider_for(base_url: str) -> HTTPToolProvider:
    return HTTPToolProvider.static({CONNECTION: local_dev_connection(base_url)})


def create_binding() -> ResolvedToolBinding:
    resolved = build_agent().resolve("scheduling.create")
    assert resolved is not None
    return resolved


CREATE_ARGS: dict[str, Any] = {
    "service_code": "HC-01",
    "starts_at": "2026-10-06T13:00:00+00:00",
    "hours": 0.5,
}


def with_timeout(binding: ResolvedToolBinding, seconds: float) -> ResolvedToolBinding:
    return binding.model_copy(
        update={"tool": binding.tool.model_copy(update={"timeout_seconds": seconds})}
    )


async def test_sends_trusted_context_headers_from_tool_context_only(api: ApiHandle) -> None:
    await provider_for(api.base_url).execute(availability_binding(), tool_args(), CONTEXT)
    headers = api.availability_requests()[0]["headers"]
    assert isinstance(headers, dict)
    assert headers["x-tenant-id"] == CONTEXT.tenant_id
    assert headers["x-conversation-id"] == CONTEXT.conversation_id
    assert headers["x-trace-id"] == CONTEXT.trace_id
    assert headers["x-invocation-id"] == CONTEXT.invocation_id


async def test_maps_args_to_query_params(api: ApiHandle) -> None:
    await provider_for(api.base_url).execute(availability_binding(), tool_args("CN-01"), CONTEXT)
    query = api.availability_requests()[0]["query"]
    assert query == {
        "service_code": "CN-01",
        "start": "2026-10-06",
        "end": "2026-10-06",
        "limit": "20",
    }


async def test_read_5xx_is_retryable_technical_error(api: ApiHandle) -> None:
    api.state.fault = {"status": 503}
    result = await provider_for(api.base_url).execute(availability_binding(), tool_args(), CONTEXT)
    assert result.status == "technical_error"
    assert result.error is not None and result.error.retryable
    assert result.provider_metadata["http_status"] == 503


async def test_read_timeout_is_timeout(api: ApiHandle) -> None:
    api.state.fault = {"delay": 1.0}
    binding = with_timeout(availability_binding(), 0.2)
    result = await provider_for(api.base_url).execute(binding, tool_args(), CONTEXT)
    assert result.status == "timeout"


async def test_write_timeout_is_unknown_never_a_safe_error(api: ApiHandle) -> None:  # INV-006
    api.state.fault = {"delay": 1.0}
    binding = with_timeout(create_binding(), 0.2)
    result = await provider_for(api.base_url).execute(binding, CREATE_ARGS, CONTEXT)
    assert result.status == "unknown"
    assert result.error is not None and not result.error.retryable


async def test_write_5xx_is_unknown(api: ApiHandle) -> None:
    api.state.fault = {"status": 500}
    result = await provider_for(api.base_url).execute(create_binding(), CREATE_ARGS, CONTEXT)
    assert result.status == "unknown"


async def test_write_to_unreachable_system_is_safe_technical_error() -> None:
    """Connection refused: the request provably never left, so retrying is safe."""
    provider = provider_for(f"http://127.0.0.1:{unused_port()}")
    result = await provider.execute(create_binding(), CREATE_ARGS, CONTEXT)
    assert result.status == "technical_error"
    assert result.error is not None and result.error.retryable
    assert result.provider_metadata["request_sent"] is False


async def test_error_never_leaks_raw_body(api: ApiHandle) -> None:
    api.state.fault = {"status": 503}
    result = await provider_for(api.base_url).execute(availability_binding(), tool_args(), CONTEXT)
    assert "injected fault" not in result.model_dump_json()


@pytest.mark.parametrize("bad_path", ["http://evil.example/x", "//evil.example/x", "no-slash"])
async def test_non_relative_tool_path_is_a_configuration_error_not_a_validation_error(
    bad_path: str, api: ApiHandle
) -> None:
    """The defect is in the ToolDefinition, not in the caller's arguments."""
    binding = availability_binding()
    # the definition refuses these now; a spec built around that check is still stopped here
    spec = HTTPRequestSpec.model_construct(
        method="GET", path=bad_path, query=(), body=(), ignored=()
    )
    tool = binding.tool.model_copy(update={"http": spec})
    result = await provider_for(api.base_url).execute(
        binding.model_copy(update={"tool": tool}), {}, CONTEXT
    )
    assert result.status == "technical_error"
    assert result.error is not None and result.error.code == "INVALID_TOOL_CONFIGURATION"
    assert api.requests == []


def path_param_binding() -> ResolvedToolBinding:
    binding = availability_binding()
    spec = HTTPRequestSpec(method="GET", path="/bookings/{id}")
    return binding.model_copy(update={"tool": binding.tool.model_copy(update={"http": spec})})


@pytest.mark.parametrize(
    "value",
    ["..", ".", "", "a/b", "a\\b", "a?x=1", "a#f", "a%2Fb", "a%2e%2e", "a b", "a\nb", "ü"],
)
async def test_illegal_path_parameter_values_are_rejected_before_any_request(
    value: str, api: ApiHandle
) -> None:
    """A path parameter can never introduce a new segment, query or fragment, nor rely on the
    server percent-decoding it into one."""
    result = await provider_for(api.base_url).execute(path_param_binding(), {"id": value}, CONTEXT)
    assert result.status == "validation_error"
    assert result.error is not None and result.error.code == "INVALID_PATH_PARAMETER"
    assert api.requests == []


async def test_legal_path_parameter_stays_a_single_segment(api: ApiHandle) -> None:
    await provider_for(api.base_url).execute(path_param_binding(), {"id": "bk_1-2.3~x"}, CONTEXT)
    assert api.requests[-1]["path"] == "/bookings/bk_1-2.3~x"
    assert api.requests[-1]["query"] == {}


async def test_missing_path_parameter_is_a_configuration_error(api: ApiHandle) -> None:
    result = await provider_for(api.base_url).execute(path_param_binding(), {}, CONTEXT)
    assert result.status == "technical_error"
    assert result.error is not None and result.error.code == "INVALID_TOOL_CONFIGURATION"
    assert api.requests == []


async def _loopback(host: str, port: int) -> list[str]:
    return ["127.0.0.1"]


async def test_does_not_follow_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://evil.example/"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    provider = HTTPToolProvider.static(
        {CONNECTION: local_dev_connection("http://x")}, client=client, resolve_host=_loopback
    )
    result: ToolResult = await provider.execute(availability_binding(), tool_args(), CONTEXT)
    assert result.status == "technical_error"
    assert result.error is not None and result.error.code == "EXTERNAL_UNEXPECTED_STATUS"


async def test_204_no_content_is_a_known_success_not_an_unusable_response(api: ApiHandle) -> None:
    """DELETE answers 204: for a write, a body-less 2xx must not degrade into `unknown`."""
    created = httpx.post(
        f"{api.base_url}/bookings",
        json={"service_code": "HC-01", "starts_at": "2026-10-06T13:00:00+00:00", "hours": 0.5},
        headers={"Idempotency-Key": "k-delete-test"},
    )
    assert created.status_code == 201
    base = create_binding()
    spec = HTTPRequestSpec(method="DELETE", path="/bookings/{id}")
    tool = base.tool.model_copy(update={"http": spec, "output_model": None})
    binding = base.model_copy(update={"tool": tool})

    result = await provider_for(api.base_url).execute(
        binding, {"id": created.json()["id"]}, CONTEXT
    )
    assert result.status == "success" and result.data == {}
    assert api.state.bookings[created.json()["id"]]["state"] == "cancelled"


async def test_the_contact_id_reaches_the_api_only_when_the_connection_opts_in(
    api: ApiHandle,
) -> None:  # personal data: off by default, turned on per connection by the operator
    await provider_for(api.base_url).execute(availability_binding(), tool_args(), CONTEXT)
    plain = api.availability_requests()[0]["headers"]
    assert isinstance(plain, dict) and "x-contact-id" not in plain

    opted = local_dev_connection(api.base_url).model_copy(update={"send_contact_id": True})
    await HTTPToolProvider.static({CONNECTION: opted}).execute(
        availability_binding(), tool_args(), CONTEXT
    )
    headers = api.availability_requests()[1]["headers"]
    assert isinstance(headers, dict) and headers["x-contact-id"] == CONTEXT.contact_id
