from __future__ import annotations

import time
from collections.abc import Callable, Hashable

import httpx
from pydantic import ValidationError

from conversation_agent.adapters.circuit import CircuitBreaker
from conversation_agent.adapters.tool_selection.http_contract import (
    CONTRACT_VERSION,
    SelectRequest,
    SelectResponse,
    WireTool,
)
from conversation_agent.adapters.tools.guard import GuardedTransport, Rejected
from conversation_agent.core.definitions.tool_selection import HttpSelectorSettings
from conversation_agent.core.errors import ToolSelectorCircuitOpenError, ToolSelectorError
from conversation_agent.core.models.llm import LLMToolDefinition
from conversation_agent.ports.metrics import ToolMetrics
from conversation_agent.ports.tool_selector import ToolScore


class _Failure(ToolSelectorError):
    """A failed call; `health` says whether it counts against the circuit (the server is unwell)
    or not (our configuration, or an answer that proves the server is alive)."""

    def __init__(self, message: str, *, health: bool) -> None:
        super().__init__(message)
        self.health = health


class HttpToolSelector:
    """Ranks tools by asking an external server (contract: `http_contract.py`).

    It goes out through the same `GuardedTransport` as tools: a pre-registered per-tenant
    connection, HTTPS unless the connection says otherwise, SSRF/DNS-pinning checks, credential from
    the SecretProvider, bounded response, no redirects. Every failure raises `ToolSelectorError`
    (never with the message text or a secret in it); the engine then sends the full list.

    A circuit breaker per (tenant, connection) stops calling a server that keeps failing (health
    failures only: unreachable, timeout, 5xx/429, unusable answer); while open, `rank` raises
    `ToolSelectorCircuitOpenError` without any I/O."""

    def __init__(
        self,
        guard: GuardedTransport,
        settings: HttpSelectorSettings,
        *,
        top_n: int,
        metrics: ToolMetrics | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._guard = guard
        self._settings = settings
        self._top_n = top_n
        self._breaker: CircuitBreaker | None = None
        circuit = settings.circuit
        if circuit.enabled:

            def record(key: Hashable, state: str) -> None:
                if metrics is not None:
                    metrics.record_circuit(
                        provider="tool_selector", scope=settings.connection, state=state
                    )

            self._breaker = CircuitBreaker(
                failure_threshold=circuit.failure_threshold,
                open_seconds=circuit.open_seconds,
                half_open_probes=circuit.half_open_probes,
                on_state=record,
                monotonic=monotonic,
            )

    def circuit_state(self, tenant_id: str) -> str:
        return "closed" if self._breaker is None else self._breaker.state(self._key(tenant_id))

    def _key(self, tenant_id: str) -> tuple[str, str]:
        return (tenant_id, self._settings.connection)

    async def rank(
        self, message: str, candidates: tuple[LLMToolDefinition, ...], *, tenant_id: str
    ) -> list[ToolScore]:
        key = self._key(tenant_id)
        admission = self._breaker.admit(key) if self._breaker is not None else "pass"
        if admission == "refuse":
            raise ToolSelectorCircuitOpenError("selector circuit is open; not called")
        try:
            ranked = await self._ask(message, candidates, tenant_id)
        except _Failure as failure:
            self._settle(key, healthy=not failure.health, probing=admission == "probe")
            raise
        except BaseException:
            self._settle(key, healthy=False, probing=admission == "probe")
            raise
        self._settle(key, healthy=True, probing=admission == "probe")
        return ranked

    def _settle(self, key: tuple[str, str], *, healthy: bool, probing: bool) -> None:
        if self._breaker is not None:
            self._breaker.settle(key, healthy=healthy, probing=probing)

    async def _ask(
        self, message: str, candidates: tuple[LLMToolDefinition, ...], tenant_id: str
    ) -> list[ToolScore]:
        payload = self._payload(message, candidates)
        try:
            connection = await self._guard.connection(tenant_id, self._settings.connection, None)
            if len(payload) > connection.max_request_bytes:
                raise _Failure("request too large", health=False)
            url = httpx.URL(connection.base_url.rstrip("/") + self._settings.path)
            request_url, host_header, sni = await self._guard.pin(url, connection)
            headers = {"Content-Type": "application/json", "X-Tenant-Id": tenant_id}
            if host_header is not None:
                headers["Host"] = host_header
            await self._guard.apply_auth(headers, connection, tenant_id)
        except Rejected as rejected:
            code = rejected.result.error.code if rejected.result.error else "REJECTED"
            # our configuration (or a DNS blip before anything was sent): not the server's fault
            raise _Failure(f"selector connection refused: {code}", health=False) from None

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
            raise _Failure(f"selector unreachable: {type(exc).__name__}", health=True) from None
        try:
            content = await self._guard.read_bounded(response, connection.max_response_bytes)
        except httpx.HTTPError as exc:
            raise _Failure(f"selector read failed: {type(exc).__name__}", health=True) from None
        finally:
            await response.aclose()
        status = response.status_code
        if status != 200:
            # 5xx/429: the server is unwell. Other statuses (401, 404, a redirect) prove it answers.
            raise _Failure(
                f"selector answered HTTP {status}", health=status >= 500 or status == 429
            )
        if content is None:
            raise _Failure("selector answer too large", health=True)
        ranked = self._scores(content, candidates)
        sent = {t.name for t in candidates}
        return [ToolScore(name, ranked.get(name, 0.0)) for name in sorted(sent)]

    # The two hooks a provider speaking another wire format overrides (see `jev.py`).
    def _payload(self, message: str, candidates: tuple[LLMToolDefinition, ...]) -> bytes:
        body = SelectRequest(
            message=message,
            tools=[WireTool(name=t.name, description=t.description) for t in candidates],
            top_n=self._top_n,
        )
        return body.model_dump_json().encode()

    def _scores(
        self, content: bytes, candidates: tuple[LLMToolDefinition, ...]
    ) -> dict[str, float]:
        try:
            answer = SelectResponse.model_validate_json(content)
        except ValidationError:
            raise _Failure("selector answer is not valid", health=True) from None
        if answer.version != CONTRACT_VERSION:
            raise _Failure(f"selector speaks contract v{answer.version}", health=True)
        sent = {t.name for t in candidates}
        return {s.name: s.score for s in answer.scores if s.name in sent}
