"""The outbound-HTTP hardening every tool transport shares (DESIGN §30-31).

HTTP and MCP tools reach external systems the same way, so they get the same guarantees from ONE
place, not from two copies that drift apart:

  - the destination is always a pre-registered, per-tenant connection (never a model-supplied URL);
  - HTTPS unless the connection says otherwise; host allowlist;
  - SSRF: every resolved address must be public unless the connection allows private networks;
    ONE mixed or blocked answer rejects the whole destination;
  - DNS rebinding: the validated IP is pinned for the request (Host header + TLS SNI keep the
    original name);
  - credentials come from a SecretProvider at call time, never overwrite a runtime-owned header;
  - response size is bounded while streaming.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable

import httpx

from conversation_agent.core.definitions.binding import ErrorMap
from conversation_agent.core.errors import (
    ConnectionNotFoundError,
    InvalidConnectionError,
    SecretNotFoundError,
)
from conversation_agent.core.models.connections import ResolvedConnection
from conversation_agent.core.models.tooling import ToolError, ToolResult
from conversation_agent.ports.connections import ConnectionResolver
from conversation_agent.ports.secrets import SecretProvider
from conversation_agent.tools.error_mapping import classify_timeout

HostResolver = Callable[[str, int], Awaitable[list[str]]]
# Pre-send DNS failures are always a safe technical_error: the binding's map is irrelevant.
_NO_MAP = ErrorMap()


def validation_error(code: str, message: str) -> ToolResult:
    """The *business arguments* are unusable (e.g. an illegal id value)."""
    return ToolResult(status="validation_error", error=ToolError(code=code, message_safe=message))


def configuration_error(code: str, message: str) -> ToolResult:
    """The ToolDefinition/connection/destination is not acceptable: nothing was sent."""
    return ToolResult(status="technical_error", error=ToolError(code=code, message_safe=message))


class Rejected(Exception):
    """Raised before any I/O: carries the canonical result of a request that was never sent."""

    def __init__(self, result: ToolResult) -> None:
        super().__init__(result.error.code if result.error else "rejected")
        self.result = result


async def system_resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def is_blocked(address: str) -> bool:
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


class GuardedTransport:
    def __init__(
        self,
        connections: ConnectionResolver,
        secrets: SecretProvider | None = None,
        client: httpx.AsyncClient | None = None,
        resolve_host: HostResolver | None = None,
    ) -> None:
        self._connections = connections
        self._secrets = secrets
        self.client = client or httpx.AsyncClient(follow_redirects=False)
        self._resolve_host = resolve_host or system_resolve

    async def aclose(self) -> None:
        await self.client.aclose()

    async def destination_fingerprint(
        self, tenant_id: str, connection_id: str | None
    ) -> str | None:
        """Where requests would go for this tenant right now (no secrets). Raises
        `ConnectionNotFoundError` if the tenant has no such connection."""
        if connection_id is None:
            return None
        resolved = await self._connections.resolve(tenant_id, connection_id)
        try:
            return resolved.fingerprint()
        except ValueError as exc:  # e.g. a connection built without validation
            raise InvalidConnectionError(f"connection is unusable: {exc}") from exc

    async def connection(
        self, tenant_id: str, connection_id: str | None, expected_destination: str | None
    ) -> ResolvedConnection:
        """The connection to use, or `Rejected` (nothing sent) when it is missing, invalid or no
        longer points where the operation was prepared (INV-027)."""
        if connection_id is None:
            raise Rejected(configuration_error("CONNECTION_NOT_CONFIGURED", "No connection."))
        try:
            connection = await self._connections.resolve(tenant_id, connection_id)
        except ConnectionNotFoundError:
            raise Rejected(
                configuration_error("CONNECTION_NOT_CONFIGURED", "No connection.")
            ) from None
        try:
            current = connection.fingerprint()
        except ValueError:
            raise Rejected(
                configuration_error("INVALID_CONNECTION_CONFIGURATION", "Connection is invalid.")
            ) from None
        if expected_destination is not None and current != expected_destination:
            # The operation was prepared against another destination (INV-027): never send it
            # somewhere else just because configuration changed in the meantime.
            raise Rejected(
                configuration_error(
                    "DESTINATION_CHANGED",
                    "The connection no longer points where this operation was prepared.",
                )
            )
        return connection

    async def pin(
        self, url: httpx.URL, connection: ResolvedConnection
    ) -> tuple[httpx.URL, str | None, str | None]:
        """Validate scheme/host/IPs and return (url to request, Host header, SNI name)."""
        host = url.host
        if connection.tls_required and url.scheme != "https":
            raise Rejected(configuration_error("INSECURE_CONNECTION", "HTTPS is required."))
        base_host = httpx.URL(connection.base_url).host
        allowed = connection.allowed_hosts or frozenset({base_host})
        if host not in allowed:
            raise Rejected(configuration_error("HOST_NOT_ALLOWED", "Destination host not allowed."))
        port = url.port or (443 if url.scheme == "https" else 80)

        try:
            ipaddress.ip_address(host)
            addresses, is_literal = [host], True
        except ValueError:
            is_literal = False
            try:
                addresses = await self._resolve_host(host, port)
            except OSError:
                raise Rejected(
                    classify_timeout(_NO_MAP, "write", request_sent=False)
                ) from None  # DNS failure: not sent, safe to retry
        if not addresses:
            raise Rejected(classify_timeout(_NO_MAP, "write", request_sent=False))
        if not connection.allow_private_networks and any(is_blocked(a) for a in addresses):
            raise Rejected(
                configuration_error("DESTINATION_NOT_ALLOWED", "Destination address not allowed.")
            )
        if is_literal:
            return url, None, None
        pinned = addresses[0]
        netloc_ip = f"[{pinned}]" if ":" in pinned else pinned
        pinned_url = url.copy_with(host=netloc_ip.strip("[]"))
        return pinned_url, host, host if url.scheme == "https" else None

    async def apply_auth(
        self, headers: dict[str, str], connection: ResolvedConnection, tenant_id: str
    ) -> None:
        auth = connection.auth
        if auth is None:
            return
        if self._secrets is None:
            raise Rejected(configuration_error("SECRET_NOT_AVAILABLE", "No secret provider."))
        try:
            secret = await self._secrets.get(tenant_id, auth.secret_ref)
        except SecretNotFoundError:
            raise Rejected(
                configuration_error("SECRET_NOT_AVAILABLE", "Credential not available.")
            ) from None
        if auth.header.casefold() in {h.casefold() for h in headers}:
            # defence in depth: AuthSpec already refuses runtime-owned names, but a credential
            # can never replace a header the runtime has already set
            raise Rejected(
                configuration_error("INVALID_AUTH_CONFIGURATION", "Auth header is runtime-owned.")
            )
        headers[auth.header] = (
            f"{auth.scheme} {secret.reveal()}" if auth.scheme else secret.reveal()
        )

    @staticmethod
    async def read_bounded(response: httpx.Response, limit: int) -> bytes | None:
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > limit:
                return None
            chunks.append(chunk)
        return b"".join(chunks)
