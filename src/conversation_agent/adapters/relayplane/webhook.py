"""Inbound webhook (DESIGN 17 and 40): verify, normalise, PERSIST, only then answer 2xx.

    raw body + headers
      -> size limit -> subscription (tenant/channel come from HERE, not from the payload)
      -> HMAC-SHA256 over "<t>.<raw body>" with a replay window (constant-time compare)
      -> strict schema -> InboundEvent -> InboxStore.insert_if_absent (dedupe by event id)
      -> 200   (duplicate or new: the gateway delivers at-least-once, we ack both)

A storage failure is a 5xx: the gateway retries, and nothing is ever acknowledged that was not
persisted. Unknown event types are acknowledged (and ignored) so they are not retried forever.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from conversation_agent.core.errors import ConversationIdentityConflictError, SecretNotFoundError
from conversation_agent.core.models.media import MAX_MEDIA_ITEMS, MediaReference
from conversation_agent.core.models.runtime import InboundEvent
from conversation_agent.ports.clock import Clock
from conversation_agent.ports.inbox import InboxStore
from conversation_agent.ports.secrets import SecretProvider
from conversation_agent.ports.subscriptions import Subscription, SubscriptionResolver

log = logging.getLogger(__name__)

SIGNATURE_HEADER = "x-relay-signature"
MAX_BODY_BYTES = 256 * 1024
MAX_TEXT_CHARS = 8000
_SIGNATURE = re.compile(r"^t=(\d{1,12}),(v1=[0-9a-f]{64}(?:,v1=[0-9a-f]{64})*)$")


@dataclass(frozen=True)
class WebhookResponse:
    status: int
    body: dict[str, Any] = field(default_factory=dict)


class _RelayInbound(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=200)
    type: str
    channel_id: str = Field(min_length=1, max_length=200)
    conversation_id: str = Field(min_length=1, max_length=200)
    contact_id: str = Field(min_length=1, max_length=200)
    session_id: str | None = Field(default=None, max_length=200)
    occurred_at: AwareDatetime
    sequence: int | None = None
    reply_to: str | None = Field(default=None, max_length=200)
    text: str | None = Field(default=None, max_length=MAX_TEXT_CHARS)
    media: list[MediaReference] = Field(default_factory=list, max_length=MAX_MEDIA_ITEMS)


class RelayPlaneWebhook:
    def __init__(
        self,
        inbox: InboxStore,
        subscriptions: SubscriptionResolver,
        secrets: SecretProvider,
        clock: Clock,
        *,
        tolerance: timedelta = timedelta(minutes=5),
        max_body_bytes: int = MAX_BODY_BYTES,
    ) -> None:
        self._inbox = inbox
        self._subscriptions = subscriptions
        self._secrets = secrets
        self._clock = clock
        self._tolerance = tolerance
        self._max_body = max_body_bytes

    async def handle(
        self, subscription_id: str, body: bytes, headers: Mapping[str, str]
    ) -> WebhookResponse:
        if len(body) > self._max_body:
            return WebhookResponse(413, {"error": "payload_too_large"})
        subscription = await self._subscriptions.resolve(subscription_id)
        if subscription is None:
            return WebhookResponse(404, {"error": "unknown_subscription"})
        rejected = await self._verify(subscription, body, headers)
        if rejected is not None:
            return rejected
        try:
            payload = _RelayInbound.model_validate(json.loads(body))
        except (ValueError, ValidationError):
            return WebhookResponse(400, {"error": "invalid_payload"})
        if payload.type != "message.received":
            return WebhookResponse(200, {"status": "ignored"})  # acknowledged, never retried
        if payload.channel_id != subscription.channel_id:
            return WebhookResponse(400, {"error": "channel_mismatch"})
        if not (payload.text or payload.media):
            return WebhookResponse(400, {"error": "empty_message"})

        now = self._clock.now()
        event = InboundEvent(
            tenant_id=subscription.tenant_id,  # from the registration, never the payload
            channel_id=subscription.channel_id,
            event_id=payload.id,
            conversation_id=payload.conversation_id,
            contact_id=payload.contact_id,
            session_id=payload.session_id or payload.conversation_id,
            text=payload.text or "",
            occurred_at=payload.occurred_at,
            received_at=now,
            source_sequence=payload.sequence,
            provider_occurred_at=payload.occurred_at,  # the channel's own clock (INV-026)
            reply_to_provider_message_id=payload.reply_to,
            media=tuple(payload.media),
        )
        try:
            created = await self._inbox.insert_if_absent(event)
        except ConversationIdentityConflictError:
            return WebhookResponse(409, {"error": "conversation_identity_conflict"})
        except Exception:
            # Not persisted -> NOT acknowledged: the gateway will deliver it again.
            log.exception("inbound event %s could not be persisted", payload.id)
            return WebhookResponse(503, {"error": "storage_unavailable"})
        return WebhookResponse(200, {"status": "accepted" if created else "duplicate"})

    async def _verify(
        self, subscription: Subscription, body: bytes, headers: Mapping[str, str]
    ) -> WebhookResponse | None:
        header = next((v for k, v in headers.items() if k.lower() == SIGNATURE_HEADER), None)
        parsed = _SIGNATURE.match(header.replace(" ", "")) if header else None
        if parsed is None:
            return WebhookResponse(401, {"error": "missing_or_malformed_signature"})
        timestamp = int(parsed[1])
        if abs(self._clock.now().timestamp() - timestamp) > self._tolerance.total_seconds():
            return WebhookResponse(401, {"error": "stale_signature"})  # replay window
        try:
            secret = (
                await self._secrets.get(subscription.tenant_id, subscription.secret_ref)
            ).reveal()
        except SecretNotFoundError:
            log.error("webhook secret for %s is not configured", subscription.subscription_id)
            return WebhookResponse(503, {"error": "signature_not_configurable"})
        expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
        candidates = [part[3:] for part in parsed[2].split(",")]  # several v1= allow rotation
        if not any(hmac.compare_digest(expected.hexdigest(), c) for c in candidates):
            return WebhookResponse(401, {"error": "invalid_signature"})
        return None
