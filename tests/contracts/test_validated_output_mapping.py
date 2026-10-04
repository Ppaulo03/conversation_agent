"""Phase 5.3: the output mapping reads what the Tool schema ACCEPTED, so what the compiler
reasons about (the Tool's declared output) is exactly what the runtime maps."""

from __future__ import annotations

from typing import Any

import pytest

from conversation_agent.adapters.tools.fake import FakeToolProvider
from conversation_agent.core.compiler import compile_manifest
from conversation_agent.core.models.tooling import ToolResult
from conversation_agent.engine.tool_runner import ToolRunner
from conversation_agent.tools.requests import build_capability_request

from .test_tool_provider_contract import CONTEXT


def agent(*, risk: str = "read") -> Any:
    return compile_manifest(
        {
            "agent_id": "measure",
            "version": "1.0.0",
            "persona": "x",
            "capabilities": [
                {
                    "name": "demo.measure",
                    "description": "measure",
                    "risk": risk,
                    "confirmation_required": risk != "read",
                    "input": {"x": {"type": "string"}},
                    "output": {"hours": {"type": "number"}, "label": {"type": "string"}},
                }
            ],
            "tools": [
                {
                    "name": "measure_tool",
                    "description": "measure",
                    "risk": risk,
                    "confirmation_required": risk != "read",
                    "idempotency_supported": risk != "read",
                    "provider": "http",
                    "connection": "c",
                    "http": {
                        "method": "POST" if risk != "read" else "GET",
                        "path": "/m",
                        "body": ["x"],
                    }
                    if risk != "read"
                    else {"method": "GET", "path": "/m", "query": ["x"]},
                    "input": {"x": {"type": "string"}},
                    "output": {
                        "minutes": {"type": "number"},
                        "name": {"type": "string"},
                        "note": {"type": "string", "required": False, "default": "n/a"},
                    },
                }
            ],
            "bindings": [
                {
                    "capability": "demo.measure",
                    "tool": "measure_tool",
                    "input_map": {"x": "$.x"},
                    "output_map": {
                        "hours": {"from": "$.minutes", "transform": "minutes_to_hours"},
                        "label": "$.note",
                    },
                    "error_map": {"default_5xx": {"write": {"type": "unknown"}}}
                    if risk != "read"
                    else {},
                }
            ],
            "allowed_capabilities": ["demo.measure"],
        }
    )


async def run(risk: str, data: dict[str, Any]) -> Any:
    compiled = agent(risk=risk)
    resolved = compiled.agent.resolve("demo.measure")
    request = build_capability_request(resolved.capability, {"x": "a"})
    provider = FakeToolProvider({"measure_tool": ToolResult(status="success", data=data)})
    return await ToolRunner({"http": provider}).run(resolved, request, CONTEXT)


async def test_output_mapping_uses_the_validated_tool_response() -> None:
    # "90" is a valid number for the Tool schema (Pydantic coerces it); the raw JSON would make
    # minutes_to_hours divide a string
    result = await run("read", {"minutes": "90", "name": "n"})
    assert result.status == "success", result
    assert result.data == {"hours": 1.5, "label": "n/a"}  # the schema default took part too


async def test_a_coercible_valid_write_response_never_becomes_unknown() -> None:
    result = await run("irreversible", {"minutes": "60", "name": "n", "unrelated": [1, 2]})
    assert result.status == "success", result  # the effect happened and the answer is valid
    assert result.data is not None and result.data["hours"] == 1.0


async def test_a_response_the_tool_schema_rejects_is_still_a_mapping_failure() -> None:
    result = await run("irreversible", {"minutes": "not a number", "name": "n"})
    assert result.status == "unknown"  # write + unusable answer: reconciled, never assumed
    result = await run("read", {"minutes": "not a number", "name": "n"})
    assert result.status == "technical_error"


@pytest.mark.parametrize("extra", [{}, {"surprise": {"nested": True}}])
async def test_fields_the_tool_did_not_declare_never_reach_the_mapping(
    extra: dict[str, Any],
) -> None:
    result = await run("read", {"minutes": 30, "name": "n", **extra})
    assert result.status == "success" and result.data == {"hours": 0.5, "label": "n/a"}
