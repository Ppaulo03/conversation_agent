"""An MCP tool is part of the agent's identity: it travels in the manifest, the pinned schema
digest is content, and an agent without MCP tools keeps the digest it always had."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from conversation_agent.core.compiler import CompileError, compile_manifest
from vertical_slice.wiring import MANIFEST_PATH, load_compiled_agent

from .test_compiler import load_manifest_file


def raw() -> dict[str, Any]:
    return copy.deepcopy(load_manifest_file(MANIFEST_PATH))


def as_mcp(manifest: dict[str, Any], digest: str = "a" * 64) -> dict[str, Any]:
    tool = manifest["tools"][0]
    tool.pop("http")
    tool["provider"] = "mcp"
    tool["mcp"] = {"remote_name": "get_slots", "schema_digest": digest}
    return manifest


def test_an_agent_without_mcp_tools_keeps_its_digest() -> None:
    assert compile_manifest(raw()).digest == load_compiled_agent().digest
    document = compile_manifest(raw()).agent
    assert all(t.mcp is None for t in document.tools)


def test_an_mcp_tool_in_a_manifest_compiles_and_re_pinning_is_a_new_agent() -> None:
    first = compile_manifest(as_mcp(raw()))
    repinned = compile_manifest(as_mcp(raw(), digest="b" * 64))
    assert first.digest != repinned.digest != load_compiled_agent().digest
    tool = next(t for t in first.agent.tools if t.provider == "mcp")
    assert tool.mcp is not None and tool.mcp.remote_name == "get_slots"


def test_an_mcp_tool_without_its_spec_or_connection_is_refused() -> None:
    no_spec = as_mcp(raw())
    no_spec["tools"][0].pop("mcp")
    no_connection = as_mcp(raw())
    no_connection["tools"][0].pop("connection")
    bad_digest = as_mcp(raw(), digest="not-a-digest")
    for manifest in (no_spec, no_connection, bad_digest):
        with pytest.raises((CompileError, ValueError)):
            compile_manifest(manifest)
