"""Phase 14 (audit): a tool's static path cannot leave the connection's base path."""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from conversation_agent.adapters.manifest.yaml_loader import load_manifest_file
from conversation_agent.adapters.tools.http import HTTPToolProvider, local_dev_connection
from conversation_agent.core.compiler import CompileError, compile_manifest
from conversation_agent.core.definitions.tool import HTTPRequestSpec
from conversation_agent.core.http_path import http_path_problem
from vertical_slice.definitions import CONNECTION
from vertical_slice.wiring import MANIFEST_PATH

from .test_tool_provider_contract import CONTEXT, availability_binding, tool_args

GOOD = ["/availability", "/bookings/{id}", "/bookings/by-key/{key}/", "/v1/a-b_c.d", "/"]
BAD = [
    "availability",  # not rooted
    "/../admin",
    "/a/../../admin",
    "/%2e%2e/admin",
    "/%2E%2e/admin",
    "/a/./b",
    "/a/%2e/b",
    "//evil",
    "http://evil/x",
    "/foo?admin=true",
    "/foo#fragment",
    "/a/b%2fc",
    "/a\b",
    "/a b",
    "/a//b",
    "/\x00x",
    "/%00",  # decoded, a control character: proxies and servers disagree on it
    "/%09",
    "/%20",
    "/%0a",
    "/%3f",  # an encoded query mark
    "/%23",  # an encoded fragment mark
    "/a%2",  # malformed escape
    "/%zz",
]


@pytest.mark.parametrize("path", GOOD)
def test_a_path_under_the_connection_is_accepted(path: str) -> None:
    assert http_path_problem(path) is None
    HTTPRequestSpec(method="GET", path=path)


@pytest.mark.parametrize("path", BAD)
def test_a_path_that_could_leave_the_connection_is_refused_by_the_definition(path: str) -> None:
    assert http_path_problem(path) is not None
    with pytest.raises(ValidationError, match="path"):
        HTTPRequestSpec(method="GET", path=path)


def test_the_compiler_refuses_it_for_a_manifest_too() -> None:
    raw = load_manifest_file(MANIFEST_PATH)
    tool = next(t for t in raw["tools"] if "http" in t)
    tool["http"]["path"] = "/../admin"
    with pytest.raises(CompileError, match="path"):
        compile_manifest(raw)


def test_the_normalisation_really_would_have_escaped_the_base_path() -> None:
    # why the rule exists: a URL stack resolves the dot segments and lands outside /v1
    assert str(httpx.URL("https://api.example.com/v1/../admin")) == "https://api.example.com/admin"


async def test_the_adapter_refuses_a_hand_built_spec_without_sending(api) -> None:  # type: ignore[no-untyped-def]
    binding = availability_binding()
    evil = HTTPRequestSpec.model_construct(
        method="GET", path="/../admin", query=(), body=(), ignored=()
    )
    tool = binding.tool.model_copy(update={"http": evil})
    provider = HTTPToolProvider.static({CONNECTION: local_dev_connection(api.base_url)})
    result = await provider.execute(binding.model_copy(update={"tool": tool}), tool_args(), CONTEXT)
    assert result.status == "technical_error" and result.error.code == "INVALID_TOOL_CONFIGURATION"  # type: ignore[union-attr]
    assert api.requests == []  # nothing left the process
