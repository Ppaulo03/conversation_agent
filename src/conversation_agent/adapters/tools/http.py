"""HTTPToolProvider: executes an HTTP tool against a *pre-registered, per-tenant connection*.

Hardening (DESIGN §30-31):
  - the URL is always `connection.base_url + a relative tool path`; no absolute URLs, no
    host/scheme override by the model or the tool definition;
  - HTTPS required unless the connection says otherwise; host allowlist;
  - SSRF: every resolved address must be public unless the connection explicitly allows private
    networks; ONE mixed or blocked answer rejects the whole destination;
  - DNS rebinding: the validated IP is pinned for the request (Host header + TLS SNI keep the
    original name), so a second DNS answer cannot redirect it;
  - redirects are never followed; request and response sizes are bounded; the timeout can only
    be tightened by the connection, never widened by the tool;
  - credentials come from a SecretProvider at call time and never appear in results, errors or
    metadata.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import quote

import httpx

from conversation_agent.adapters.connections.static import StaticConnectionResolver
from conversation_agent.core.definitions.binding import ErrorMap, ResolvedToolBinding
from conversation_agent.core.errors import ConnectionNotFoundError, SecretNotFoundError
from conversation_agent.core.models.connections import ResolvedConnection
from conversation_agent.core.models.tooling import ToolContext, ToolError, ToolResult
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

HostResolver = Callable[[str, int], Awaitable[list[str]]]
# Pre-send DNS failures are always a safe technical_error: the binding's map is irrelevant.
_NO_MAP = ErrorMap()


def local_dev_connection(base_url: str, connection_id: str = "dev") -> ResolvedConnection:
    """A deliberately permissive connection for localhost development and tests ONLY."""
    return ResolvedConnection(
        connection_id=connection_id,
        base_url=base_url,
        allow_private_networks=True,
        tls_required=False,
    )


def _validation_error(code: str, message: str) -> ToolResult:
    """The *business arguments* are unusable (e.g. an illegal id value)."""
    return ToolResult(status="validation_error", error=ToolError(code=code, message_safe=message))


def _configuration_error(code: str, message: str) -> ToolResult:
    """The ToolDefinition/connection/destination is not acceptable: nothing was sent."""
    return ToolResult(status="technical_error", error=ToolError(code=code, message_safe=message))


class _IllegalPathValue(ValueError):
    pass


class _Rejected(Exception):
    def __init__(self, result: ToolResult) -> None:
        super().__init__(result.error.code if result.error else "rejected")
        self.result = result


async def _system_resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def _is_blocked(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:10.0.0.1 is still 10.0.0.1
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


class HTTPToolProvider:
    def __init__(
        self,
        connections: ConnectionResolver,
        secrets: SecretProvider | None = None,
        client: httpx.AsyncClient | None = None,
        resolve_host: HostResolver | None = None,
    ) -> None:
        self._connections = connections
        self._secrets = secrets
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._resolve_host = resolve_host or _system_resolve

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
        await self._client.aclose()

    async def destination_fingerprint(
        self, binding: ResolvedToolBinding, context: ToolContext
    ) -> str | None:
        """Where this tool's requests would go for this tenant right now (no secrets). Raises
        `ConnectionNotFoundError` if the tenant has no such connection."""
        if binding.tool.connection is None:
            return None
        resolved = await self._connections.resolve(context.tenant_id, binding.tool.connection)
        return resolved.fingerprint()

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
        except _Rejected as rejected:  # nothing was sent: a known, safe failure
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
        if spec is None or tool.connection is None:
            raise _Rejected(_configuration_error("CONNECTION_NOT_CONFIGURED", "No connection."))
        try:
            connection = await self._connections.resolve(context.tenant_id, tool.connection)
        except ConnectionNotFoundError:
            raise _Rejected(
                _configuration_error("CONNECTION_NOT_CONFIGURED", "No connection.")
            ) from None
        if expected_destination is not None and connection.fingerprint() != expected_destination:
            # The operation was prepared against another destination (INV-027): never send it
            # somewhere else just because configuration changed in the meantime.
            raise _Rejected(
                _configuration_error(
                    "DESTINATION_CHANGED",
                    "The connection no longer points where this operation was prepared.",
                )
            )
        if not spec.path.startswith("/") or spec.path.startswith("//") or "://" in spec.path:
            raise _Rejected(
                _configuration_error(
                    "INVALID_TOOL_CONFIGURATION", "Tool path must be relative to its connection."
                )
            )

        path_params = set(_PATH_PARAM.findall(spec.path))
        missing = sorted(p for p in path_params if args.get(p) is None)
        if missing:
            raise _Rejected(
                _configuration_error(
                    "INVALID_TOOL_CONFIGURATION", f"Path parameter(s) not provided: {missing}."
                )
            )
        try:
            path = _PATH_PARAM.sub(lambda m: self._path_value(args, m.group(1)), spec.path)
        except _IllegalPathValue as exc:
            raise _Rejected(_validation_error("INVALID_PATH_PARAMETER", str(exc))) from exc
        query = {k: args[k] for k in spec.query if args.get(k) is not None and k not in path_params}
        body = {k: args[k] for k in spec.body if k in args and k not in path_params}
        payload = json.dumps(body).encode() if body else None
        if payload is not None and len(payload) > connection.max_request_bytes:
            raise _Rejected(_configuration_error("REQUEST_TOO_LARGE", "Request body too large."))

        url = httpx.URL(connection.base_url.rstrip("/") + path)
        request_url, host_header, sni = await self._pin_destination(url, connection)

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
        await self._apply_auth(headers, connection, context.tenant_id)

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
            content = await self._read_bounded(response, connection.max_response_bytes)
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

    # ------------------------------------------------------------------ security

    async def _pin_destination(
        self, url: httpx.URL, connection: ResolvedConnection
    ) -> tuple[httpx.URL, str | None, str | None]:
        """Validate scheme/host/IPs and return (url to request, Host header, SNI name)."""
        host = url.host
        if connection.tls_required and url.scheme != "https":
            raise _Rejected(_configuration_error("INSECURE_CONNECTION", "HTTPS is required."))
        base_host = httpx.URL(connection.base_url).host
        allowed = connection.allowed_hosts or frozenset({base_host})
        if host not in allowed:
            raise _Rejected(
                _configuration_error("HOST_NOT_ALLOWED", "Destination host not allowed.")
            )
        port = url.port or (443 if url.scheme == "https" else 80)

        try:
            ipaddress.ip_address(host)
            addresses, is_literal = [host], True
        except ValueError:
            is_literal = False
            try:
                addresses = await self._resolve_host(host, port)
            except OSError:
                raise _Rejected(
                    classify_timeout(_NO_MAP, "write", request_sent=False)
                ) from None  # DNS failure: not sent, safe to retry
        if not addresses:
            raise _Rejected(classify_timeout(_NO_MAP, "write", request_sent=False))
        if not connection.allow_private_networks and any(_is_blocked(a) for a in addresses):
            raise _Rejected(
                _configuration_error("DESTINATION_NOT_ALLOWED", "Destination address not allowed.")
            )
        if is_literal:
            return url, None, None
        pinned = addresses[0]
        netloc_ip = f"[{pinned}]" if ":" in pinned else pinned
        pinned_url = url.copy_with(host=netloc_ip.strip("[]"))
        return pinned_url, host, host if url.scheme == "https" else None

    async def _apply_auth(
        self, headers: dict[str, str], connection: ResolvedConnection, tenant_id: str
    ) -> None:
        auth = connection.auth
        if auth is None:
            return
        if self._secrets is None:
            raise _Rejected(_configuration_error("SECRET_NOT_AVAILABLE", "No secret provider."))
        try:
            secret = await self._secrets.get(tenant_id, auth.secret_ref)
        except SecretNotFoundError:
            raise _Rejected(
                _configuration_error("SECRET_NOT_AVAILABLE", "Credential not available.")
            ) from None
        headers[auth.header] = (
            f"{auth.scheme} {secret.reveal()}" if auth.scheme else secret.reveal()
        )

    @staticmethod
    async def _read_bounded(response: httpx.Response, limit: int) -> bytes | None:
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > limit:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _path_value(args: dict[str, Any], name: str) -> str:
        value = str(args[name])
        if value in (".", "..") or not _SEGMENT_VALUE.fullmatch(value):
            raise _IllegalPathValue(f"illegal value for path parameter {name!r}")
        return quote(value, safe="")
