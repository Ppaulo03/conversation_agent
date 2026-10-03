"""HTTPToolProvider: executes an HTTP tool against a *pre-registered connection*.

Phase 1 scope. The URL is always `connection.base_url + tool path`; neither the LLM nor the
tool supplies an absolute URL. Full SSRF/secret hardening (ConnectionResolver, DNS/private
network policy, size limits) is Phase 3.
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict

from conversation_agent.core.definitions.binding import ResolvedToolBinding
from conversation_agent.core.models.tooling import ToolContext, ToolError, ToolResult
from conversation_agent.tools.error_mapping import (
    classify_http_status,
    classify_invalid_response,
    classify_timeout,
)

_PATH_PARAM = re.compile(r"\{(\w+)\}")


class HTTPConnection(BaseModel):
    model_config = ConfigDict(frozen=True)

    base_url: str
    default_headers: dict[str, str] = {}


def _validation_error(code: str, message: str) -> ToolResult:
    """The *business arguments* are unusable (e.g. an illegal id value)."""
    return ToolResult(status="validation_error", error=ToolError(code=code, message_safe=message))


def _configuration_error(code: str, message: str) -> ToolResult:
    """The ToolDefinition/connection is broken: not the caller's fault, nothing was sent."""
    return ToolResult(status="technical_error", error=ToolError(code=code, message_safe=message))


class _IllegalPathValue(ValueError):
    pass


# A path *segment* value: unreserved characters only. No "/", "\\", "?", "#", "%" (which would
# let a server decode a new segment/query out of one parameter), no whitespace or controls.
_SEGMENT_VALUE = re.compile(r"[A-Za-z0-9._~-]+")


class HTTPToolProvider:
    def __init__(
        self,
        connections: dict[str, HTTPConnection],
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._connections = connections
        self._client = client or httpx.AsyncClient(follow_redirects=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def execute(
        self, binding: ResolvedToolBinding, args: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        tool = binding.tool
        spec = tool.http
        connection = self._connections.get(tool.connection or "")
        if spec is None or connection is None:
            return _configuration_error("CONNECTION_NOT_CONFIGURED", "No connection.")
        if not spec.path.startswith("/") or spec.path.startswith("//") or "://" in spec.path:
            return _configuration_error(
                "INVALID_TOOL_CONFIGURATION", "Tool path must be relative to its connection."
            )

        path_params = set(_PATH_PARAM.findall(spec.path))
        missing = sorted(p for p in path_params if args.get(p) is None)
        if missing:
            return _configuration_error(
                "INVALID_TOOL_CONFIGURATION", f"Path parameter(s) not provided: {missing}."
            )
        try:
            path = _PATH_PARAM.sub(lambda m: self._path_value(args, m.group(1)), spec.path)
        except _IllegalPathValue as exc:
            return _validation_error("INVALID_PATH_PARAMETER", str(exc))
        query = {k: args[k] for k in spec.query if args.get(k) is not None and k not in path_params}
        body = {k: args[k] for k in spec.body if k in args and k not in path_params}

        headers = {
            **connection.default_headers,
            "X-Tenant-Id": context.tenant_id,
            "X-Conversation-Id": context.conversation_id,
            "X-Trace-Id": context.trace_id,
            "X-Invocation-Id": context.invocation_id,
        }
        if binding.effective_risk != "read" and tool.idempotency_supported:
            # The key is the stable invocation identity: the same on every technical retry,
            # replay and reconciliation, never regenerated (INV-021).
            headers["Idempotency-Key"] = context.invocation_id
        started = time.monotonic()
        try:
            response = await self._client.request(
                spec.method,
                connection.base_url.rstrip("/") + path,
                params=query or None,
                json=body or None,
                headers=headers,
                timeout=httpx.Timeout(tool.timeout_seconds),
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.UnsupportedProtocol):
            return classify_timeout(
                binding.binding.error_map, binding.effective_risk, request_sent=False
            )
        except httpx.RequestError:
            # Timeout / broken connection after the request may have been delivered.
            return classify_timeout(
                binding.binding.error_map, binding.effective_risk, request_sent=True
            )

        metadata = {
            "http_status": response.status_code,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
        failure = classify_http_status(
            response.status_code, binding.binding.error_map, binding.effective_risk
        )
        if failure is not None:
            return failure.model_copy(
                update={"provider_metadata": {**failure.provider_metadata, **metadata}}
            )
        try:
            data = response.json()
        except ValueError:
            return classify_invalid_response(binding.effective_risk)
        if not isinstance(data, dict | list):
            return classify_invalid_response(binding.effective_risk)
        return ToolResult(status="success", data=data, provider_metadata=metadata)

    @staticmethod
    def _path_value(args: dict[str, Any], name: str) -> str:
        value = str(args[name])
        if value in (".", "..") or not _SEGMENT_VALUE.fullmatch(value):
            raise _IllegalPathValue(f"illegal value for path parameter {name!r}")
        return quote(value, safe="")
