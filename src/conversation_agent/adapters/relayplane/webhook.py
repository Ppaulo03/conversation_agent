"""Inbound webhook of the RelayPlane gateway (docs/RELAYPLANE_CONTRACT.md; DESIGN 17 and 40).

    raw body + headers
      -> size -> subscription (OUR tenant comes from the registration, not from the payload)
      -> signature: HMAC-SHA256 over "<X-RelayPlane-Timestamp>.<raw body>", `v1=` entries (several
         while a secret rotates), replay window, constant-time compare
      -> envelope (`schema_version` 1, `event_id`, `sequence`, ...) -> by `event_type`:

           message.received         InboundEvent -> InboxStore.insert_if_absent (dedupe by event_id)
           message.outbound_status  what became of a send we made -> OutboxStore (forward only)
           message.deleted          withdraw the event(s) not yet claimed by a turn
           message.status, instance.status_changed, anything else: acknowledged, ignored

A 2xx is only ever sent after the effect is DURABLE; a storage failure is a 5xx and the gateway
redelivers (same `event_id`, same `sequence`). Delivery is at-least-once: duplicates are normal.
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

from conversation_agent.adapters.senders.relayplane import result_from_gateway
from conversation_agent.core.errors import ConversationIdentityConflictError, SecretNotFoundError
from conversation_agent.core.models.media import MAX_MEDIA_ITEMS, MediaReference
from conversation_agent.core.models.runtime import InboundEvent
from conversation_agent.core.observability import bind
from conversation_agent.ports.admission import AdmissionControl
from conversation_agent.ports.clock import Clock
from conversation_agent.ports.inbox import InboxStore
from conversation_agent.ports.outbox import OutboxStore
from conversation_agent.ports.secrets import SecretProvider
from conversation_agent.ports.subscriptions import Subscription, SubscriptionResolver

log = logging.getLogger(__name__)

SUPPORTED_SCHEMA_VERSION = 1
MAX_BODY_BYTES = 256 * 1024
MAX_TEXT_CHARS = 8000
H_TIMESTAMP = "x-relayplane-timestamp"
H_SIGNATURE = "x-relayplane-signature"
H_EVENT_ID = "x-relayplane-event-id"
_V1 = re.compile(r"v1=([0-9a-f]{64})")
_MEDIA_KINDS = {"audio": "audio", "image": "image", "video": "video", "document": "document",
                "sticker": "image"}  # fmt: skip


@dataclass(frozen=True)
class WebhookResponse:
    status: int
    body: dict[str, Any] = field(default_factory=dict)
    retry_after: int | None = None  # seconds, for a refusal the gateway should retry later


class _Envelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: int
    event_id: str = Field(min_length=1, max_length=200)
    sequence: int | None = None
    event_type: str
    tenant_id: str = Field(min_length=1, max_length=200)
    instance_id: str = Field(min_length=1, max_length=200)
    timestamp: AwareDatetime
    payload: dict[str, Any]


class _Media(BaseModel):
    model_config = ConfigDict(extra="ignore")

    media_id: str = Field(min_length=1, max_length=200)
    status: str
    reason: str | None = None
    kind: str
    mime_type: str = Field(default="application/octet-stream", max_length=100)
    size: int | None = Field(default=None, ge=0)
    seconds: int | None = Field(default=None, ge=0)
    filename: str | None = Field(default=None, max_length=200)


class _Received(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider_message_id: str = Field(min_length=1, max_length=200)
    reply_to_provider_message_id: str | None = Field(default=None, max_length=200)
    from_: str = Field(alias="from", min_length=1, max_length=64)
    type: str
    text: str | None = Field(default=None, max_length=MAX_TEXT_CHARS)
    chat_id: str | None = None  # present for group chats
    media: _Media | None = None


class RelayPlaneWebhook:
    def __init__(
        self,
        inbox: InboxStore,
        outbox: OutboxStore,
        subscriptions: SubscriptionResolver,
        secrets: SecretProvider,
        clock: Clock,
        *,
        tolerance: timedelta = timedelta(minutes=5),
        max_body_bytes: int = MAX_BODY_BYTES,
        admission: AdmissionControl | None = None,
    ) -> None:
        self._admission = admission
        self._inbox = inbox
        self._outbox = outbox
        self._subscriptions = subscriptions
        self._secrets = secrets
        self._clock = clock
        self._tolerance = tolerance
        self.max_body_bytes = max_body_bytes

    async def handle(
        self, subscription_id: str, body: bytes, headers: Mapping[str, str]
    ) -> WebhookResponse:
        if len(body) > self.max_body_bytes:
            return WebhookResponse(413, {"error": "payload_too_large"})
        subscription = await self._subscriptions.resolve(subscription_id)
        if subscription is None:
            return WebhookResponse(404, {"error": "unknown_subscription"})
        lowered = {k.lower(): v for k, v in headers.items()}
        rejected = await self._verify(subscription, body, lowered)
        if rejected is not None:
            return rejected
        try:
            envelope = _Envelope.model_validate(json.loads(body))
        except (ValueError, ValidationError):
            return WebhookResponse(400, {"error": "invalid_envelope"})
        if envelope.schema_version != SUPPORTED_SCHEMA_VERSION:
            return WebhookResponse(400, {"error": "unsupported_schema_version"})
        header_id = lowered.get(H_EVENT_ID)
        if header_id is not None and header_id != envelope.event_id:
            return WebhookResponse(400, {"error": "event_id_mismatch"})
        if subscription.relay_tenant_id and envelope.tenant_id != subscription.relay_tenant_id:
            return WebhookResponse(400, {"error": "tenant_mismatch"})
        if subscription.instance_ids and envelope.instance_id not in subscription.instance_ids:
            return WebhookResponse(200, {"status": "ignored", "reason": "instance_not_subscribed"})
        with bind(
            tenant_id=subscription.tenant_id,
            channel_id=envelope.instance_id,
            event_id=envelope.event_id,
            component="webhook",
        ):
            try:
                return await self._dispatch(subscription, envelope)
            except ConversationIdentityConflictError:
                log.warning("webhook.conversation_identity_conflict")
                return WebhookResponse(409, {"error": "conversation_identity_conflict"})
            except Exception:
                # Not persisted -> NOT acknowledged: the gateway will deliver it again.
                log.exception(
                    "webhook.not_applied", extra={"fields": {"event_type": envelope.event_type}}
                )
                return WebhookResponse(503, {"error": "storage_unavailable"})

    # ------------------------------------------------------------------ events

    async def _dispatch(self, sub: Subscription, event: _Envelope) -> WebhookResponse:
        if event.event_type == "message.received":
            return await self._received(sub, event)
        if event.event_type == "message.outbound_status":
            return await self._outbound_status(sub, event)
        if event.event_type == "message.deleted":
            pid = event.payload.get("provider_message_id")
            if not isinstance(pid, str) or not pid:
                return WebhookResponse(400, {"error": "invalid_payload"})
            withdrawn = await self._inbox.withdraw_unprocessed(
                sub.tenant_id, event.instance_id, pid
            )
            return WebhookResponse(200, {"status": "applied", "withdrawn": withdrawn})
        if event.event_type == "instance.status_changed":
            log.warning(
                "webhook.gateway_instance_status",
                extra={"fields": {"status": str(event.payload.get("status"))}},
            )
        return WebhookResponse(200, {"status": "ignored"})  # acknowledged, never retried

    async def _received(self, sub: Subscription, event: _Envelope) -> WebhookResponse:
        try:
            msg = _Received.model_validate(event.payload)
        except ValidationError:
            return WebhookResponse(400, {"error": "invalid_payload"})
        if msg.chat_id is not None:
            return WebhookResponse(200, {"status": "ignored", "reason": "group"})  # out of scope
        if msg.type == "secretEncrypted":
            # an edit whose new text we cannot read: nothing to process (and nothing to trust)
            return WebhookResponse(200, {"status": "ignored", "reason": "unreadable_edit"})
        media: tuple[MediaReference, ...] = ()
        if msg.media is not None:
            kind = _MEDIA_KINDS.get(msg.media.kind)
            status = msg.media.status.lower()
            if kind is None or status not in ("ready", "rejected", "failed"):
                return WebhookResponse(400, {"error": "invalid_media"})
            media = (
                MediaReference(
                    media_id=msg.media.media_id,
                    kind=kind,
                    mime_type=msg.media.mime_type,
                    size_bytes=msg.media.size,
                    seconds=msg.media.seconds,
                    filename=msg.media.filename,
                    status=status,
                    reason=msg.media.reason,
                ),
            )
        if not (msg.text or media):
            return WebhookResponse(400, {"error": "empty_message"})
        if self._admission is not None:
            # Backpressure at the edge, BEFORE anything is persisted or acknowledged: a refused
            # message is redelivered by the gateway, so this costs latency, never data.
            decision = await self._admission.admit(sub.tenant_id, msg.from_)
            if not decision.admitted:
                return WebhookResponse(
                    decision.status, {"error": decision.reason}, decision.retry_after_seconds
                )
        assert len(media) <= MAX_MEDIA_ITEMS
        conversation = f"{event.instance_id}:{msg.from_}"
        created = await self._inbox.insert_if_absent(
            InboundEvent(
                tenant_id=sub.tenant_id,  # from the registration, never the payload
                channel_id=event.instance_id,
                event_id=event.event_id,
                conversation_id=conversation,
                contact_id=msg.from_,
                session_id=conversation,
                text=msg.text or "",
                occurred_at=event.timestamp,  # the PROVIDER's clock for a received message
                received_at=self._clock.now(),
                source_sequence=event.sequence,  # the gateway's delivery order, gap-free
                provider_occurred_at=event.timestamp,
                reply_to_provider_message_id=msg.reply_to_provider_message_id,
                provider_message_id=msg.provider_message_id,
                media=media,
            )
        )
        return WebhookResponse(200, {"status": "accepted" if created else "duplicate"})

    async def _outbound_status(self, sub: Subscription, event: _Envelope) -> WebhookResponse:
        p = event.payload
        message_id, status = p.get("message_id"), p.get("status")
        if not isinstance(message_id, str) or not isinstance(status, str):
            return WebhookResponse(400, {"error": "invalid_payload"})
        result = result_from_gateway(
            status,
            channel_message_id=message_id,
            provider_message_id=p.get("provider_message_id")
            if isinstance(p.get("provider_message_id"), str)
            else None,
            accepted_at=p.get("accepted_at") if isinstance(p.get("accepted_at"), str) else None,
        )
        if result is None:
            return WebhookResponse(200, {"status": "ignored", "reason": "unknown_status"})
        applied = await self._outbox.apply_channel_status(sub.tenant_id, message_id, result)
        return WebhookResponse(200, {"status": "applied" if applied else "no_change"})

    # ------------------------------------------------------------------ signature

    async def _verify(
        self, sub: Subscription, body: bytes, headers: Mapping[str, str]
    ) -> WebhookResponse | None:
        try:
            timestamp = int(headers[H_TIMESTAMP])
            signature = headers[H_SIGNATURE]
        except (KeyError, ValueError):
            return WebhookResponse(401, {"error": "missing_or_malformed_signature"})
        candidates = _V1.findall(signature)
        if not candidates:
            return WebhookResponse(401, {"error": "missing_or_malformed_signature"})
        if abs(self._clock.now().timestamp() - timestamp) > self._tolerance.total_seconds():
            return WebhookResponse(401, {"error": "stale_signature"})  # replay window
        try:
            secret = (await self._secrets.get(sub.tenant_id, sub.secret_ref)).reveal()
        except SecretNotFoundError:
            log.error(
                "webhook.secret_not_configured",
                extra={"fields": {"alert": True, "subscription": sub.subscription_id}},
            )
            return WebhookResponse(503, {"error": "signature_not_configurable"})
        expected = hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256)
        if not any(hmac.compare_digest(expected.hexdigest(), c) for c in candidates):
            return WebhookResponse(401, {"error": "invalid_signature"})
        return None
