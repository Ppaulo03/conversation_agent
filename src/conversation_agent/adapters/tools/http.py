"""HTTPToolProvider: executes an HTTP tool against a *pre-registered, per-tenant connection*.

Hardening (DESIGN §30-31) lives in `guard.GuardedTransport`, shared with the MCP provider:
  - the URL is always `connection.base_url + a relative tool path`; no absolute URLs, no
    host/scheme override by the model or the tool definition;
  - HTTPS required unless the connection says otherwise; host allowlist;
  - SSRF: every resolved address must be public unless the connection explicitly allows private
    networks; DNS rebinding: the validated IP is pinned for the request;
  - redirects are never followed; request and response sizes are bounded; the timeout can only
    be tightened by the connection, never widened by the tool;
  - credentials come from a SecretProvider at call time and never appear in results, errors or
    metadata.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from conversation_agent.adapters.connections.static import StaticConnectionResolver
from conversation_agent.adapters.tools.guard import (
    GuardedTransport,
    HostResolver,
    Rejected,
    configuration_error,
    validation_error,
)
from conversation_agent.core.definitions.binding import ResolvedToolBinding
from conversation_agent.core.models.connections import ResolvedConnection
from conversation_agent.core.models.tooling import ToolContext, ToolResult
from conversation_agent.ports.connections import ConnectionResolver
from conversation_agent.ports.secrets import SecretProvider
from conversation_agent.tools.error_mapping import (
    classify_http_status,
    classify_invalid_response,
    classify_timeout,
)

_PATH_PARAM = re.compile(r"\{(\w+)\}")
# A path *segment* value: unreserved characters only. No "/", "\\", "?", "#", "%" (which would
# let a server decode a new segment/query out of one parameter), no whitespace or controls.
_SEGMENT_VALUE = re.compile(r"[A-Za-z0-9._~-]+")

__all__ = ["HTTPToolProvider", "HostResolver", "local_dev_connection"]


def local_dev_connection(base_url: str, connection_id: str = "dev") -> ResolvedConnection:
    """A deliberately permissive connection for localhost development and tests ONLY."""
    return ResolvedConnection(
        connection_id=connection_id,
        base_url=base_url,
        allow_private_networks=True,
        tls_required=False,
    )


class _IllegalPathValue(ValueError):
    pass


class HTTPToolProvider:
    def __init__(
        self,
        connections: ConnectionResolver,
        secrets: SecretProvider | None = None,
        client: httpx.AsyncClient | None = None,
        resolve_host: HostResolver | None = None,
    ) -> None:
        self._guard = GuardedTransport(connections, secrets, client, resolve_host)
        self._client = self._guard.client

    @classmethod
    def static(
        cls,
        connections: Mapping[str | tuple[str, str], ResolvedConnection],
        secrets: SecretProvider | None = None,
        client: httpx.AsyncClient | None = None,
        resolve_host: HostResolver | None = None,
    ) -> HTTPToolProvider:
        return cls(StaticConnectionResolver(connections), secrets, client, resolve_host)

    async def aclose(self) -> None:
        await self._guard.aclose()

    async def destination_fingerprint(
        self, binding: ResolvedToolBinding, context: ToolContext
    ) -> str | None:
        """Where this tool's requests would go for this tenant right now (no secrets). Raises
        `ConnectionNotFoundError` if the tenant has no such connection."""
        return await self._guard.destination_fingerprint(context.tenant_id, binding.tool.connection)

    async def execute(
        self,
        binding: ResolvedToolBinding,
        args: dict[str, Any],
        context: ToolContext,
        *,
        destination_fingerprint: str | None = None,
    ) -> ToolResult:
        try:
            return await self._execute(binding, args, context, destination_fingerprint)
        except Rejected as rejected:  # nothing was sent: a known, safe failure
            return rejected.result

    async def _execute(
        self,
        binding: ResolvedToolBinding,
        args: dict[str, Any],
        context: ToolContext,
        expected_destination: str | None,
    ) -> ToolResult:
        tool = binding.tool
        spec = tool.http
        if spec is None:
            raise Rejected(configuration_error("CONNECTION_NOT_CONFIGURED", "No connection."))
        connection = await self._guard.connection(
            context.tenant_id, tool.connection, expected_destination
        )
        if not spec.path.startswith("/") or spec.path.startswith("//") or "://" in spec.path:
            raise Rejected(
                configuration_error(
                    "INVALID_TOOL_CONFIGURATION", "Tool path must be relative to its connection."
                )
            )

        path_params = set(_PATH_PARAM.findall(spec.path))
        missing = sorted(p for p in path_params if args.get(p) is None)
        if missing:
            raise Rejected(
                configuration_error(
                    "INVALID_TOOL_CONFIGURATION", f"Path parameter(s) not provided: {missing}."
                )
            )
        try:
            path = _PATH_PARAM.sub(lambda m: self._path_value(args, m.group(1)), spec.path)
        except _IllegalPathValue as exc:
            raise Rejected(validation_error("INVALID_PATH_PARAMETER", str(exc))) from exc
        query = {k: args[k] for k in spec.query if args.get(k) is not None and k not in path_params}
        body = {k: args[k] for k in spec.body if k in args and k not in path_params}
        payload = json.dumps(body).encode() if body else None
        if payload is not None and len(payload) > connection.max_request_bytes:
            raise Rejected(configuration_error("REQUEST_TOO_LARGE", "Request body too large."))

        url = httpx.URL(connection.base_url.rstrip("/") + path)
        request_url, host_header, sni = await self._guard.pin(url, connection)

        headers = {
            "X-Tenant-Id": context.tenant_id,
            "X-Conversation-Id": context.conversation_id,
            "X-Trace-Id": context.trace_id,
            "X-Invocation-Id": context.invocation_id,
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if host_header is not None:
            headers["Host"] = host_header
        if binding.effective_risk != "read" and tool.idempotency_supported:
            # The key is the stable invocation identity: the same on every technical retry,
            # replay and reconciliation, never regenerated (INV-021).
            headers["Idempotency-Key"] = context.invocation_id
        await self._guard.apply_auth(headers, connection, context.tenant_id)

        timeout = min(tool.timeout_seconds, connection.max_timeout_seconds)
        started = time.monotonic()
        try:
            request = self._client.build_request(
                spec.method,
                request_url,
                params=query or None,
                content=payload,
                headers=headers,
                timeout=httpx.Timeout(timeout),
                extensions={"sni_hostname": sni} if sni else None,
            )
            response = await self._client.send(request, stream=True, follow_redirects=False)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.UnsupportedProtocol):
            return classify_timeout(
                binding.binding.error_map, binding.effective_risk, request_sent=False
            )
        except httpx.RequestError:
            # Timeout / broken connection after the request may have been delivered.
            return classify_timeout(
                binding.binding.error_map, binding.effective_risk, request_sent=True
            )

        try:
            content = await self._guard.read_bounded(response, connection.max_response_bytes)
        except httpx.RequestError:
            return classify_timeout(
                binding.binding.error_map, binding.effective_risk, request_sent=True
            )
        finally:
            await response.aclose()
        if content is None:  # oversized: refuse to buffer it
            return classify_invalid_response(binding.effective_risk)

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
        if response.status_code == 204 or not content:
            # A known success with no body (e.g. DELETE): not an "unusable response".
            return ToolResult(status="success", data={}, provider_metadata=metadata)
        try:
            data = json.loads(content)
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
