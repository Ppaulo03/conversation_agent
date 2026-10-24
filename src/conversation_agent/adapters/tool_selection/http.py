from __future__ import annotations

import httpx
from pydantic import ValidationError

from conversation_agent.adapters.tool_selection.http_contract import (
    CONTRACT_VERSION,
    SelectRequest,
    SelectResponse,
    WireTool,
)
from conversation_agent.adapters.tools.guard import GuardedTransport, Rejected
from conversation_agent.core.definitions.tool_selection import HttpSelectorSettings
from conversation_agent.core.errors import ToolSelectorError
from conversation_agent.core.models.llm import LLMToolDefinition
from conversation_agent.ports.tool_selector import ToolScore


class HttpToolSelector:
    """Ranks tools by asking an external server (contract: `http_contract.py`).

    It goes out through the same `GuardedTransport` as tools: a pre-registered per-tenant
    connection, HTTPS unless the connection says otherwise, SSRF/DNS-pinning checks, credential from
    the SecretProvider, bounded response, no redirects. Every failure raises `ToolSelectorError`
    (never with the message text or a secret in it); the engine then sends the full list."""

    def __init__(
        self, guard: GuardedTransport, settings: HttpSelectorSettings, *, top_n: int
    ) -> None:
        self._guard = guard
        self._settings = settings
        self._top_n = top_n

    async def rank(
        self, message: str, candidates: tuple[LLMToolDefinition, ...], *, tenant_id: str
    ) -> list[ToolScore]:
        body = SelectRequest(
            message=message,
            tools=[WireTool(name=t.name, description=t.description) for t in candidates],
            top_n=self._top_n,
        )
        payload = body.model_dump_json().encode()
        try:
            connection = await self._guard.connection(tenant_id, self._settings.connection, None)
            if len(payload) > connection.max_request_bytes:
                raise ToolSelectorError("request too large")
            url = httpx.URL(connection.base_url.rstrip("/") + self._settings.path)
            request_url, host_header, sni = await self._guard.pin(url, connection)
            headers = {"Content-Type": "application/json", "X-Tenant-Id": tenant_id}
            if host_header is not None:
                headers["Host"] = host_header
            await self._guard.apply_auth(headers, connection, tenant_id)
        except Rejected as rejected:
            code = rejected.result.error.code if rejected.result.error else "REJECTED"
            raise ToolSelectorError(f"selector connection refused: {code}") from None

        timeout = min(self._settings.timeout_seconds, connection.max_timeout_seconds)
        try:
            request = self._guard.client.build_request(
                "POST",
                request_url,
                content=payload,
                headers=headers,
                timeout=httpx.Timeout(timeout),
                extensions={"sni_hostname": sni} if sni else None,
            )
            response = await self._guard.client.send(request, stream=True, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise ToolSelectorError(f"selector unreachable: {type(exc).__name__}") from None
        try:
            content = await self._guard.read_bounded(response, connection.max_response_bytes)
        except httpx.HTTPError as exc:
            raise ToolSelectorError(f"selector read failed: {type(exc).__name__}") from None
        finally:
            await response.aclose()
        if response.status_code != 200:
            raise ToolSelectorError(f"selector answered HTTP {response.status_code}")
        if content is None:
            raise ToolSelectorError("selector answer too large")
        try:
            answer = SelectResponse.model_validate_json(content)
        except ValidationError:
            raise ToolSelectorError("selector answer is not valid") from None
        if answer.version != CONTRACT_VERSION:
            raise ToolSelectorError(f"selector speaks contract v{answer.version}")
        sent = {t.name for t in candidates}
        ranked = {s.name: s.score for s in answer.scores if s.name in sent}
        return [ToolScore(name, ranked.get(name, 0.0)) for name in sorted(sent)]
