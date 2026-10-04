"""MessageSender over the RelayPlane gateway (docs/RELAYPLANE_CONTRACT.md).

A send is ASYNCHRONOUS: the gateway durably accepts it (202 QUEUED + its own `message_id`) and
reports the rest later (`message.outbound_status` event, or `GET /messages/{id}`):

    POST /api/v1/messages/send  (Idempotency-Key = the outbox row's key)
        202 {message_id, status: QUEUED}   -> QUEUED, `channel_message_id` recorded
        429                                -> FAILED, retryable (it did not take the message)
        400/404/409/413/422                -> FAILED, not retryable (it refused it)
        5xx, timeout, network error, unreadable answer -> UNKNOWN
    `UNKNOWN` from a transport problem is SAFE to retry with the same key while the gateway still
    remembers it (`GET /limits` -> idempotency_retention_seconds): the reconciler does exactly that.

Gateway status -> outbox: QUEUED/DISPATCHING -> QUEUED; ACCEPTED/DELIVERED/READ -> ACCEPTED (with
the provider's id and acceptance time, the evidence a quoted reply is matched against);
FAILED -> FAILED; UNKNOWN -> UNKNOWN (the gateway wants a decision: `resolve`).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx

from conversation_agent.core.errors import ConnectionNotFoundError, SecretNotFoundError
from conversation_agent.core.models.connections import ResolvedConnection
from conversation_agent.core.models.delivery import DeliveryPolicy
from conversation_agent.core.models.runtime import OutboundMessage, OutboxStatus, SendResult
from conversation_agent.ports.connections import ConnectionResolver
from conversation_agent.ports.secrets import SecretProvider

STATUS_MAP: dict[str, OutboxStatus] = {
    "QUEUED": OutboxStatus.QUEUED,
    "DISPATCHING": OutboxStatus.QUEUED,
    "ACCEPTED": OutboxStatus.ACCEPTED,
    "DELIVERED": OutboxStatus.ACCEPTED,
    "READ": OutboxStatus.ACCEPTED,
    "FAILED": OutboxStatus.FAILED,
    "UNKNOWN": OutboxStatus.UNKNOWN,
}
_UNKNOWN = SendResult(status=OutboxStatus.UNKNOWN)


def result_from_gateway(
    status: str,
    *,
    channel_message_id: str | None,
    provider_message_id: str | None = None,
    accepted_at: str | None = None,
) -> SendResult | None:
    """One mapping for every place the gateway tells us a status (response, GET, event)."""
    mapped = STATUS_MAP.get(status.upper())
    if mapped is None:
        return None  # a status this build does not know is never assumed
    parsed: datetime | None = None
    if mapped is OutboxStatus.ACCEPTED and accepted_at:
        try:
            candidate = datetime.fromisoformat(accepted_at.replace("Z", "+00:00"))
        except ValueError:
            candidate = None
        parsed = candidate if candidate is not None and candidate.tzinfo is not None else None
    return SendResult(
        status=mapped,
        provider_message_id=provider_message_id or None,
        provider_accepted_at=parsed,
        channel_message_id=channel_message_id,
    )


class RelayPlaneSender:
    def __init__(
        self,
        connections: ConnectionResolver,
        secrets: SecretProvider | None = None,
        client: httpx.AsyncClient | None = None,
        *,
        connection_id: str = "relayplane",
    ) -> None:
        self._connections = connections
        self._secrets = secrets
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._connection_id = connection_id

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ send

    async def send(self, message: OutboundMessage) -> SendResult:
        prepared = await self._prepare(message.tenant_id)
        if isinstance(prepared, SendResult):
            return prepared  # nothing was sent
        base, headers, timeout = prepared
        body = {
            "instance_id": message.channel_id,
            "to": message.contact_id,
            "type": "text",
            "payload": {"text": message.text},
        }  # deterministic from the persisted row: identical on every retry (same key, same body)
        try:
            response = await self._client.post(
                f"{base}/api/v1/messages/send",
                json=body,
                headers={**headers, "Idempotency-Key": message.idempotency_key},
                timeout=timeout,
            )
        except httpx.RequestError:
            return _UNKNOWN  # timeout/broken connection: the gateway may have it (replay is safe)
        return self._interpret(response)

    # ------------------------------------------------------------------ lookup (reconciliation)

    async def lookup(self, message: OutboundMessage) -> SendResult:
        if message.channel_message_id is None:
            raise RuntimeError("the gateway never gave an id for this message")
        prepared = await self._prepare(message.tenant_id)
        if isinstance(prepared, SendResult):
            raise RuntimeError("cannot ask the gateway: connection/credential unavailable")
        base, headers, timeout = prepared
        response = await self._client.get(
            f"{base}/api/v1/messages/{message.channel_message_id}", headers=headers, timeout=timeout
        )
        if response.status_code != 200:
            raise RuntimeError(f"gateway answered {response.status_code} to a status query")
        data = self._json(response)
        if data is None:
            raise RuntimeError("gateway status answer unreadable")
        found = result_from_gateway(
            str(data.get("status", "")),
            channel_message_id=message.channel_message_id,
            provider_message_id=_text(data.get("provider_message_id")),
            accepted_at=_text(data.get("accepted_at")),
        )
        if found is None:
            raise RuntimeError("gateway reported a status this build does not know")
        return found

    async def resolve(self, message: OutboundMessage, *, sent: bool) -> SendResult:
        """OPERATOR action for a gateway-UNKNOWN send: record whether it really left. Resolving
        the wrong way duplicates or loses the message: never call this from automation."""
        prepared = await self._prepare(message.tenant_id)
        if isinstance(prepared, SendResult) or message.channel_message_id is None:
            raise RuntimeError("cannot resolve: connection unavailable or no gateway id")
        base, headers, timeout = prepared
        response = await self._client.post(
            f"{base}/api/v1/messages/{message.channel_message_id}/resolve",
            json={"outcome": "sent" if sent else "not_sent"},
            headers=headers,
            timeout=timeout,
        )
        data = self._json(response)
        found = (
            result_from_gateway(
                str(data.get("status", "")),
                channel_message_id=message.channel_message_id,
                provider_message_id=_text(data.get("provider_message_id")),
                accepted_at=_text(data.get("accepted_at")),
            )
            if response.status_code == 200 and data is not None
            else None
        )
        if found is None:
            raise RuntimeError(f"resolve answered {response.status_code}")
        return found

    # ------------------------------------------------------------------ startup check

    async def delivery_policy(self, tenant_id: str, retry_horizon: timedelta) -> DeliveryPolicy:
        """`sender_retry_horizon <= idempotency retention`, checked against what the DEPLOYED
        gateway reports (`GET /limits`), not against a number someone typed."""
        prepared = await self._prepare(tenant_id)
        if isinstance(prepared, SendResult):
            raise RuntimeError("cannot read the gateway limits: connection/credential unavailable")
        base, headers, timeout = prepared
        response = await self._client.get(f"{base}/api/v1/limits", headers=headers, timeout=timeout)
        data = self._json(response) if response.status_code == 200 else None
        seconds = data.get("idempotency_retention_seconds") if data else None
        if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds <= 0:
            raise RuntimeError("the gateway did not report its idempotency retention")
        return DeliveryPolicy(
            idempotency_retention=timedelta(seconds=seconds), retry_horizon=retry_horizon
        )

    # ------------------------------------------------------------------ internals

    async def _prepare(
        self, tenant_id: str
    ) -> SendResult | tuple[str, dict[str, str], httpx.Timeout]:
        try:
            connection = await self._connections.resolve(tenant_id, self._connection_id)
        except ConnectionNotFoundError:
            return SendResult(status=OutboxStatus.FAILED, retryable=True)
        base = connection.base_url.rstrip("/")
        if connection.tls_required and not base.startswith("https://"):
            return SendResult(status=OutboxStatus.FAILED, retryable=False)
        headers = {"Content-Type": "application/json"}
        if connection.auth is not None:
            credential = await self._credential(connection, tenant_id)
            if credential is None:
                return SendResult(status=OutboxStatus.FAILED, retryable=True)
            headers[connection.auth.header] = credential
        return base, headers, httpx.Timeout(connection.max_timeout_seconds)

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
            data = self._json(response)
            if data is None or not data.get("message_id"):
                return _UNKNOWN
            found = result_from_gateway(
                str(data.get("status", "QUEUED")), channel_message_id=str(data["message_id"])
            )
            return found or _UNKNOWN
        if code == 429:
            return SendResult(status=OutboxStatus.FAILED, retryable=True)
        if code >= 500:
            return _UNKNOWN
        if 400 <= code < 500:
            return SendResult(status=OutboxStatus.FAILED, retryable=False)
        return _UNKNOWN

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any] | None:
        try:
            data = response.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
