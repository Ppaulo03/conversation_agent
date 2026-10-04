"""MCPToolProvider: executes a tool published by an MCP server over Streamable HTTP.

Transport is the same guarded one HTTP tools use (`guard.GuardedTransport`): a pre-registered,
per-tenant connection whose `base_url` IS the MCP endpoint, HTTPS, host allowlist, SSRF/DNS pinning,
no redirects, bounded responses, credentials from the SecretProvider. stdio servers are not
supported on purpose: spawning a process per tenant is not a transport this runtime can fence.

What MCP adds, and how it stays inside the runtime's rules:

  - A SESSION (`initialize` handshake, `Mcp-Session-Id`) is cached per (tenant, destination). A
    session that the server forgot (404) is re-opened and the call retried once: a 404 on the
    session means the call was not executed.
  - SCHEMA PIN (INV-037): before the first call per session (and every `schema_check_ttl`), the
    server's CURRENT input schema for the tool is compared with the digest pinned in the
    `MCPToolSpec`. A server that changed what the tool accepts is not the tool somebody allowed:
    nothing is sent (`MCP_TOOL_SCHEMA_CHANGED`).
  - OUTCOMES follow the canonical taxonomy: a protocol error that proves non-execution (unknown
    tool, invalid params) is safe; a tool-level `isError` or an internal error of a WRITE may have
    partly executed, so it is `unknown` unless the binding's `error_map.tool_error` says otherwise;
    a lost answer after sending is `unknown` (INV-006), never a retryable technical error.
  - Whatever the server returns (text, structured content, error text) is DATA: it is never
    interpreted as instructions and the server's error prose is not forwarded.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from conversation_agent.adapters.connections.static import StaticConnectionResolver
from conversation_agent.adapters.tools.guard import (
    GuardedTransport,
    HostResolver,
    Rejected,
    configuration_error,
)
from conversation_agent.core.definitions.binding import ErrorRule, ResolvedToolBinding
from conversation_agent.core.definitions.json_schema import clean_text, schema_digest
from conversation_agent.core.definitions.tool_import import DiscoveredTool
from conversation_agent.core.models.connections import ResolvedConnection
from conversation_agent.core.models.tooling import ToolContext, ToolError, ToolResult
from conversation_agent.ports.connections import ConnectionResolver
from conversation_agent.ports.secrets import SecretProvider
from conversation_agent.tools.error_mapping import (
    classify_http_status,
    classify_invalid_response,
    classify_timeout,
)

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = frozenset({"2025-06-18", "2025-03-26"})
MAX_LIST_PAGES = 20
_CLIENT_INFO = {"name": "conversation-agent", "version": "0.1.0"}


class MCPDiscoveryError(Exception):
    """The server's tools could not be listed (`code` is a canonical, safe error code)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Network(Exception):
    """The HTTP exchange failed; `sent` tells whether the request may have been delivered."""

    def __init__(self, sent: bool) -> None:
        super().__init__("network failure")
        self.sent = sent


class _SessionLost(Exception):
    """The server forgot the session (404): what was just sent did not run."""


@dataclass
class _Reply:
    status: int
    headers: httpx.Headers
    content: bytes


@dataclass
class _Session:
    session_id: str | None
    verified: dict[str, float] = field(default_factory=dict)  # remote tool -> monotonic stamp


def _failure(
    status: Any, code: str, message: str, *, retryable: bool = False, **meta: Any
) -> ToolResult:
    return ToolResult(
        status=status,
        error=ToolError(code=code, message_safe=message, retryable=retryable),
        provider_metadata=meta,
    )


def _jsonrpc(method: str, params: dict[str, Any] | None, request_id: int | None) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    if request_id is not None:
        body["id"] = request_id
    return body


def _message_from(reply: _Reply, request_id: int) -> dict[str, Any] | None:
    """The JSON-RPC message answering `request_id`, from a JSON or an SSE body."""
    text = reply.content.decode("utf-8", errors="replace")
    candidates: list[Any] = []
    if "text/event-stream" in reply.headers.get("content-type", ""):
        for block in text.replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(
                line[5:].lstrip() for line in block.split("\n") if line.startswith("data:")
            )
            if data:
                try:
                    candidates.append(json.loads(data))
                except ValueError:
                    continue
    else:
        try:
            candidates.append(json.loads(text))
        except ValueError:
            return None
    for item in candidates:
        if isinstance(item, dict) and item.get("id") == request_id:
            return item
    return None


class MCPToolProvider:
    def __init__(
        self,
        connections: ConnectionResolver,
        secrets: SecretProvider | None = None,
        client: httpx.AsyncClient | None = None,
        resolve_host: HostResolver | None = None,
        *,
        schema_check_ttl: float = 300.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._guard = GuardedTransport(connections, secrets, client, resolve_host)
        self._client = self._guard.client
        self._ttl = schema_check_ttl
        self._monotonic = monotonic
        self._sessions: dict[tuple[str, str], _Session] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._ids = 0

    @classmethod
    def static(
        cls,
        connections: Mapping[str | tuple[str, str], ResolvedConnection],
        secrets: SecretProvider | None = None,
        client: httpx.AsyncClient | None = None,
        resolve_host: HostResolver | None = None,
        **kw: Any,
    ) -> MCPToolProvider:
        return cls(StaticConnectionResolver(connections), secrets, client, resolve_host, **kw)

    async def aclose(self) -> None:
        await self._guard.aclose()

    async def destination_fingerprint(
        self, binding: ResolvedToolBinding, context: ToolContext
    ) -> str | None:
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

    # ------------------------------------------------------------------ the call

    async def _execute(
        self,
        binding: ResolvedToolBinding,
        args: dict[str, Any],
        context: ToolContext,
        expected_destination: str | None,
    ) -> ToolResult:
        tool = binding.tool
        spec = tool.mcp
        if spec is None:
            raise Rejected(configuration_error("INVALID_TOOL_CONFIGURATION", "Not an MCP tool."))
        connection = await self._guard.connection(
            context.tenant_id, tool.connection, expected_destination
        )
        key = (context.tenant_id, connection.fingerprint())
        started = time.monotonic()
        for _ in range(2):
            session = await self._session(key, context, connection)
            try:
                await self._verify_pin(
                    session, spec.remote_name, spec.schema_digest, context, connection
                )
                result = await self._call(binding, args, context, connection, session, started)
            except _SessionLost:
                result = None
            if result is not None:
                return result
            self._sessions.pop(key, None)  # the server forgot the session: not executed, retry
        return _failure(
            "technical_error",
            "MCP_SESSION_UNAVAILABLE",
            "The MCP session could not be kept.",
            retryable=True,
        )

    async def _call(
        self,
        binding: ResolvedToolBinding,
        args: dict[str, Any],
        context: ToolContext,
        connection: ResolvedConnection,
        session: _Session,
        started: float,
    ) -> ToolResult | None:
        """None when the server says the SESSION is gone (the call did not run)."""
        tool, risk = binding.tool, binding.effective_risk
        assert tool.mcp is not None
        extra: dict[str, str] = {}
        if risk != "read" and tool.idempotency_supported:
            extra["Idempotency-Key"] = context.invocation_id  # stable identity, never regenerated
        params: dict[str, Any] = {"name": tool.mcp.remote_name, "arguments": args}
        timeout = min(tool.timeout_seconds, connection.max_timeout_seconds)
        request_id = self._next_id()
        try:
            reply = await self._post(
                connection,
                context,
                _jsonrpc("tools/call", params, request_id),
                session,
                timeout=timeout,
                extra=extra,
            )
        except _Network as exc:
            return classify_timeout(binding.binding.error_map, risk, request_sent=exc.sent)
        if reply.status == 404 and session.session_id is not None:
            return None
        meta: dict[str, Any] = {
            "http_status": reply.status,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
        failure = classify_http_status(reply.status, binding.binding.error_map, risk)
        if failure is not None:
            return failure.model_copy(
                update={"provider_metadata": {**failure.provider_metadata, **meta}}
            )
        message = _message_from(reply, request_id)
        if message is None:
            return classify_invalid_response(risk)
        return self._interpret(message, binding, meta)

    def _interpret(
        self, message: dict[str, Any], binding: ResolvedToolBinding, meta: dict[str, Any]
    ) -> ToolResult:
        risk = binding.effective_risk
        error = message.get("error")
        if isinstance(error, dict):
            return self._protocol_error(error, binding, meta)
        result = message.get("result")
        if not isinstance(result, dict):
            return classify_invalid_response(risk)
        if result.get("isError") is True:
            # The tool RAN and reported a failure. For a write that does not prove nothing
            # happened; the server's text is prose from a stranger and is not forwarded.
            rule = binding.binding.error_map.tool_error or (
                ErrorRule(type="business_error") if risk == "read" else ErrorRule(type="unknown")
            )
            if risk != "read" and rule.type in ("technical_error", "timeout"):
                rule = ErrorRule(type="unknown", code=rule.code)  # a write may have run (INV-006)
            return ToolResult(
                status=rule.type,
                error=ToolError(
                    code=rule.code or "MCP_TOOL_ERROR",
                    message_safe="The tool reported an error.",
                    retryable=rule.retryable and rule.type in ("technical_error", "timeout"),
                ),
                provider_metadata=meta,
            )
        data = self._data(result)
        if data is None:
            return classify_invalid_response(risk)
        return ToolResult(status="success", data=data, provider_metadata=meta)

    @staticmethod
    def _protocol_error(
        error: dict[str, Any], binding: ResolvedToolBinding, meta: dict[str, Any]
    ) -> ToolResult:
        code = error.get("code")
        risk = binding.effective_risk
        if code == -32602:  # invalid params: the server refused the arguments, nothing ran
            return _failure(
                "validation_error", "MCP_INVALID_PARAMS", "The tool refused the arguments.", **meta
            )
        if code in (-32601, -32600, -32700):  # unknown method/tool, bad request: not executed
            return _failure(
                "technical_error",
                "MCP_PROTOCOL_ERROR",
                "The MCP server refused the request.",
                **meta,
            )
        if risk == "read":
            return _failure(
                "technical_error",
                "MCP_SERVER_ERROR",
                "The MCP server failed.",
                retryable=True,
                **meta,
            )
        return _failure(
            "unknown", "MCP_SERVER_ERROR", "The MCP server failed after receiving the call.", **meta
        )

    @staticmethod
    def _data(result: dict[str, Any]) -> dict[str, Any] | list[Any] | None:
        structured = result.get("structuredContent")
        if isinstance(structured, dict | list):
            return structured
        content = result.get("content", [])
        if not isinstance(content, list):
            return None
        texts = [
            str(c["text"])
            for c in content
            if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str)
        ]
        if len(texts) == 1:
            try:
                parsed = json.loads(texts[0])
            except ValueError:
                parsed = None
            if isinstance(parsed, dict | list):
                return parsed
        return {"text": "\n".join(texts)} if texts else {}

    # ------------------------------------------------------------------ session + pin

    async def _session(
        self, key: tuple[str, str], context: ToolContext, connection: ResolvedConnection
    ) -> _Session:
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            existing = self._sessions.get(key)
            if existing is not None:
                return existing
            session = await self._initialize(context, connection)
            self._sessions[key] = session
            return session

    async def _initialize(self, context: ToolContext, connection: ResolvedConnection) -> _Session:
        timeout = min(connection.max_timeout_seconds, 10.0)
        request_id = self._next_id()
        params = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": _CLIENT_INFO,
        }
        try:
            reply = await self._post(
                connection,
                context,
                _jsonrpc("initialize", params, request_id),
                _Session(None),
                timeout=timeout,
            )
        except _Network:
            raise Rejected(  # the handshake is not the operation: nothing of it was sent
                _failure(
                    "technical_error",
                    "EXTERNAL_UNREACHABLE",
                    "Could not reach the MCP server.",
                    retryable=True,
                )
            ) from None
        message = _message_from(reply, request_id) if reply.status == 200 else None
        result = message.get("result") if message else None
        if not isinstance(result, dict) or result.get("protocolVersion") not in SUPPORTED_VERSIONS:
            raise Rejected(
                _failure("technical_error", "MCP_HANDSHAKE_FAILED", "The MCP handshake failed.")
            )
        session = _Session(reply.headers.get("mcp-session-id"))
        try:
            await self._post(
                connection,
                context,
                _jsonrpc("notifications/initialized", None, None),
                session,
                timeout=timeout,
            )
        except _Network:
            raise Rejected(
                _failure(
                    "technical_error",
                    "EXTERNAL_UNREACHABLE",
                    "Could not reach the MCP server.",
                    retryable=True,
                )
            ) from None
        return session

    async def _verify_pin(
        self,
        session: _Session,
        remote_name: str,
        pinned: str,
        context: ToolContext,
        connection: ResolvedConnection,
    ) -> None:
        stamp = session.verified.get(remote_name)
        if stamp is not None and self._monotonic() - stamp < self._ttl:
            return
        digest = await self._current_digest(session, remote_name, context, connection)
        if digest is None:
            raise Rejected(
                _failure(
                    "technical_error",
                    "MCP_TOOL_NOT_FOUND",
                    "The MCP server no longer offers this tool.",
                )
            )
        if digest != pinned:
            raise Rejected(
                _failure(
                    "technical_error",
                    "MCP_TOOL_SCHEMA_CHANGED",
                    "The MCP server changed this tool's input schema; it must be reviewed again.",
                )
            )
        session.verified[remote_name] = self._monotonic()

    async def _current_digest(
        self,
        session: _Session,
        remote_name: str,
        context: ToolContext,
        connection: ResolvedConnection,
    ) -> str | None:
        for item in await self._list_tools(session, context, connection, stop_at=remote_name):
            if item.get("name") == remote_name:
                schema = item.get("inputSchema")
                return schema_digest(schema) if isinstance(schema, dict) else None
        return None

    async def _list_tools(
        self,
        session: _Session,
        context: ToolContext,
        connection: ResolvedConnection,
        *,
        stop_at: str | None = None,
    ) -> list[dict[str, Any]]:
        """The server's `tools/list`, all pages (bounded), as raw dicts: UNTRUSTED data."""
        found: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(MAX_LIST_PAGES):
            request_id = self._next_id()
            params = {"cursor": cursor} if cursor else None
            try:
                reply = await self._post(
                    connection,
                    context,
                    _jsonrpc("tools/list", params, request_id),
                    session,
                    timeout=min(connection.max_timeout_seconds, 10.0),
                )
            except _Network:
                raise Rejected(
                    _failure(
                        "technical_error",
                        "EXTERNAL_UNREACHABLE",
                        "Could not reach the MCP server.",
                        retryable=True,
                    )
                ) from None
            if reply.status == 404 and session.session_id is not None:
                raise _SessionLost
            message = _message_from(reply, request_id) if reply.status == 200 else None
            result = message.get("result") if message else None
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise Rejected(
                    _failure(
                        "technical_error",
                        "MCP_DISCOVERY_FAILED",
                        "Could not list the server's tools.",
                    )
                )
            page = [item for item in result["tools"] if isinstance(item, dict)]
            found.extend(page)
            if stop_at is not None and any(item.get("name") == stop_at for item in page):
                return found
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return found
            cursor = next_cursor
        return found

    # ------------------------------------------------------------------ discovery

    async def discover(self, tenant_id: str, connection_id: str) -> list[DiscoveredTool]:
        """What the server OFFERS. Discovery is not exposure: feed this to `import_mcp_tools`
        with an explicit allowlist; nothing here becomes a tool by itself (INV-037)."""
        context = ToolContext(
            tenant_id=tenant_id,
            agent_id="discovery",
            agent_version="0",
            channel_id="discovery",
            conversation_id="discovery",
            session_id="discovery",
            contact_id="discovery",
            turn_id="discovery",
            invocation_id="discovery",
            trace_id="discovery",
        )
        try:
            connection = await self._guard.connection(tenant_id, connection_id, None)
            session = await self._initialize(context, connection)
            items = await self._list_tools(session, context, connection)
        except Rejected as rejected:
            code = rejected.result.error.code if rejected.result.error else "REJECTED"
            raise MCPDiscoveryError(code) from None
        except _SessionLost:
            raise MCPDiscoveryError("MCP_SESSION_UNAVAILABLE") from None
        tools: list[DiscoveredTool] = []
        for item in items:
            name, schema = item.get("name"), item.get("inputSchema")
            if not isinstance(name, str) or not isinstance(schema, dict):
                continue  # a malformed entry is simply not offered
            output = item.get("outputSchema")
            annotations = item.get("annotations")
            tools.append(
                DiscoveredTool(
                    name=name,
                    description=clean_text(item.get("description")),
                    input_schema=schema,
                    output_schema=output if isinstance(output, dict) else None,
                    annotations=annotations if isinstance(annotations, dict) else {},
                )
            )
        return tools

    # ------------------------------------------------------------------ wire

    def _next_id(self) -> int:
        self._ids += 1
        return self._ids

    async def _post(
        self,
        connection: ResolvedConnection,
        context: ToolContext,
        body: dict[str, Any],
        session: _Session,
        *,
        timeout: float,
        extra: Mapping[str, str] | None = None,
    ) -> _Reply:
        payload = json.dumps(body).encode()
        if len(payload) > connection.max_request_bytes:
            raise Rejected(configuration_error("REQUEST_TOO_LARGE", "Request body too large."))
        url = httpx.URL(connection.base_url)
        request_url, host_header, sni = await self._guard.pin(url, connection)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "X-Tenant-Id": context.tenant_id,
            "X-Conversation-Id": context.conversation_id,
            "X-Trace-Id": context.trace_id,
            "X-Invocation-Id": context.invocation_id,
            **(extra or {}),
        }
        if session.session_id is not None:
            headers["Mcp-Session-Id"] = session.session_id
        if host_header is not None:
            headers["Host"] = host_header
        await self._guard.apply_auth(headers, connection, context.tenant_id)
        try:
            request = self._client.build_request(
                "POST",
                request_url,
                content=payload,
                headers=headers,
                timeout=httpx.Timeout(timeout),
                extensions={"sni_hostname": sni} if sni else None,
            )
            response = await self._client.send(request, stream=True, follow_redirects=False)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.UnsupportedProtocol):
            raise _Network(sent=False) from None
        except httpx.RequestError:
            raise _Network(sent=True) from None
        try:
            content = await self._guard.read_bounded(response, connection.max_response_bytes)
        except httpx.RequestError:
            raise _Network(sent=True) from None
        finally:
            await response.aclose()
        if content is None:  # oversized: refuse to buffer it
            return _Reply(502, response.headers, b"")
        return _Reply(response.status_code, response.headers, content)
