"""MessageSender over the channel gateway ("RelayPlane"), see docs/RELAYPLANE_CONTRACT.md.

- the row's `idempotency_key` travels as `Idempotency-Key`: a technical retry of the SAME row
  reuses it (and the same body), a semantic re-prompt is a new row with a new key;
- what the gateway answers maps to the outbox like this (DESIGN 40):

      202/200 {status: accepted}  -> ACCEPTED   (+ the gateway's own `accepted_at`, INV-026)
      202/200 {status: queued}    -> QUEUED
      4xx (not 408/409/429)       -> FAILED, not retryable (the gateway refused it)
      408/429                     -> FAILED, retryable (it did not take the message)
      409 (key in flight/reused)  -> UNKNOWN (never assume)
      5xx, timeout, network error, unreadable answer -> UNKNOWN (it may have been accepted)

- UNKNOWN is resolved by `lookup` (reconciliation), never by guessing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from conversation_agent.core.errors import ConnectionNotFoundError, SecretNotFoundError
from conversation_agent.core.models.connections import ResolvedConnection
from conversation_agent.core.models.runtime import OutboundMessage, OutboxStatus, SendResult
from conversation_agent.ports.connections import ConnectionResolver
from conversation_agent.ports.secrets import SecretProvider

_STATUS = {
    "queued": OutboxStatus.QUEUED,
    "accepted": OutboxStatus.ACCEPTED,
    "failed": OutboxStatus.FAILED,
}
_UNKNOWN = SendResult(status=OutboxStatus.UNKNOWN)


class RelayPlaneSender:
    def __init__(
        self,
        connections: ConnectionResolver,
        secrets: SecretProvider | None = None,
        client: httpx.AsyncClient | None = None,
        *,
        connection_id: str = "relayplane",
        messages_path: str = "/v1/messages",
    ) -> None:
        self._connections = connections
        self._secrets = secrets
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._connection_id = connection_id
        self._path = messages_path

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ send

    async def send(self, message: OutboundMessage) -> SendResult:
        prepared = await self._prepare(message.tenant_id)
        if isinstance(prepared, SendResult):
            return prepared  # nothing was sent
        url, headers, timeout = prepared
        body = {
            "channel_id": message.channel_id,
            "to": message.contact_id,
            "text": message.text,
        }  # deterministic from the persisted row: identical on every retry
        try:
            response = await self._client.post(
                url,
                json=body,
                headers={**headers, "Idempotency-Key": message.idempotency_key},
                timeout=timeout,
            )
        except httpx.RequestError:
            return _UNKNOWN  # timeout/broken connection: the gateway may have the message
        return self._interpret(response)

    # ------------------------------------------------------------------ lookup (reconciliation)

    async def lookup(self, message: OutboundMessage) -> SendResult | None:
        prepared = await self._prepare(message.tenant_id)
        if isinstance(prepared, SendResult):
            raise RuntimeError("cannot ask the gateway: connection/credential unavailable")
        url, headers, timeout = prepared
        response = await self._client.get(
            url,
            params={"idempotency_key": message.idempotency_key},
            headers=headers,
            timeout=timeout,
        )
        if response.status_code == 404:
            return None  # the gateway PROVES it has no such message (inside its retention)
        if response.status_code != 200:
            raise RuntimeError(f"lookup answered {response.status_code}")
        result = self._parse(response)
        if result is None:
            raise RuntimeError("lookup answer unreadable")
        return result

    # ------------------------------------------------------------------ internals

    async def _prepare(
        self, tenant_id: str
    ) -> SendResult | tuple[str, dict[str, str], httpx.Timeout]:
        try:
            connection = await self._connections.resolve(tenant_id, self._connection_id)
        except ConnectionNotFoundError:
            return SendResult(status=OutboxStatus.FAILED, retryable=True)
        url = connection.base_url.rstrip("/") + self._path
        if connection.tls_required and not url.startswith("https://"):
            return SendResult(status=OutboxStatus.FAILED, retryable=False)
        headers = {"Content-Type": "application/json"}
        credential = await self._credential(connection, tenant_id)
        if credential is None and connection.auth is not None:
            return SendResult(status=OutboxStatus.FAILED, retryable=True)
        if connection.auth is not None and credential is not None:
            headers[connection.auth.header] = credential
        return url, headers, httpx.Timeout(connection.max_timeout_seconds)

    async def _credential(self, connection: ResolvedConnection, tenant_id: str) -> str | None:
        auth = connection.auth
        if auth is None or self._secrets is None:
            return None
        try:
            secret = (await self._secrets.get(tenant_id, auth.secret_ref)).reveal()
        except SecretNotFoundError:
            return None
        return f"{auth.scheme} {secret}" if auth.scheme else secret

    def _interpret(self, response: httpx.Response) -> SendResult:
        code = response.status_code
        if code in (200, 202):
            return self._parse(response) or _UNKNOWN
        if code in (408, 429):
            return SendResult(status=OutboxStatus.FAILED, retryable=True)
        if code == 409 or code >= 500:
            return _UNKNOWN
        if 400 <= code < 500:
            return SendResult(status=OutboxStatus.FAILED, retryable=False)
        return _UNKNOWN

    @staticmethod
    def _parse(response: httpx.Response) -> SendResult | None:
        try:
            data: Any = response.json()
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        status = _STATUS.get(str(data.get("status", "")).lower())
        if status is None:
            return None
        accepted_at: datetime | None = None
        raw = data.get("accepted_at")
        if status is OutboxStatus.ACCEPTED and isinstance(raw, str):
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                parsed = None
            accepted_at = parsed if parsed is not None and parsed.tzinfo is not None else None
        provider_id = data.get("id")
        return SendResult(
            status=status,
            provider_message_id=str(provider_id) if provider_id is not None else None,
            provider_accepted_at=accepted_at,
            retryable=False,
        )
