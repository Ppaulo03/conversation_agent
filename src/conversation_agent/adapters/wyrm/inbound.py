"""Wyrm dispatch -> InboundEvent (docs/WYRM_CHANNEL.md; docs/GUIDE.md §5).

A pure mapping: the caller (the module worker that receives the dispatch) has already authenticated
it (the Wyrm core signs what it dispatches) and passes OUR `tenant_id` from its configuration. The
identity fields come from the dispatch `context`, which the core fills, never from `payload`.

    dispatch                      InboundEvent
    payload.message_id            event_id (else `dispatch_id`) and provider_message_id
    context.provider_id           channel_id
    context.chat_id               contact_id (where a reply is addressed, see below)
    (tenant, provider, chat)      conversation_id  "wyrm:<provider>:<chat>"
    timestamp                     occurred_at (the CORE's clock; falls back to the receive time)
    injected clock                received_at

`provider_occurred_at` and `reply_to_provider_message_id` stay None: the dispatch carries neither a
provider clock nor a reply-to, and the core's `timestamp` is not provider evidence (INV-026).

Returns None (nothing to hand to the runtime) for a GROUP chat, for a dispatch with no text (media
only: attachments are ignored for now) and for a dispatch that does not carry the expected shape.
A dispatch addressed to another tenant is a wiring bug and raises.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from conversation_agent.core.models.runtime import InboundEvent
from conversation_agent.ports.clock import Clock

CHANNEL_NAMESPACE = "wyrm"


def conversation_id_for(provider_id: str, chat_id: str) -> str:
    """Deterministic, namespaced per channel; ':' is refused in both parts (injective)."""
    return f"{CHANNEL_NAMESPACE}:{provider_id}:{chat_id}"


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def dispatch_to_event(
    dispatch: Mapping[str, Any], *, tenant_id: str, clock: Clock
) -> InboundEvent | None:
    context = dispatch.get("context")
    payload = dispatch.get("payload")
    if not isinstance(context, Mapping) or not isinstance(payload, Mapping):
        return None
    if context.get("tenant_id") != tenant_id:
        raise ValueError("dispatch is addressed to another tenant")
    if context.get("channel_type") != "PRIVATE":
        return None  # groups need mention/allow-list rules we do not have (docs/FUTURE_GROUPS.md)
    provider_id = _text(context.get("provider_id"))
    chat_id = _text(context.get("chat_id"))
    if provider_id is None or chat_id is None or ":" in provider_id or ":" in chat_id:
        return None  # the outbound address has the same rule: a chat we could not answer
    text = _text(payload.get("text"))
    if text is None:
        return None  # attachments are ignored: text-only for now
    message_id = _text(payload.get("message_id"))
    event_id = message_id or _text(dispatch.get("dispatch_id"))
    if event_id is None:
        return None
    received_at = clock.now()
    conversation_id = conversation_id_for(provider_id, chat_id)
    return InboundEvent(
        tenant_id=tenant_id,
        channel_id=provider_id,
        event_id=event_id,  # stable across a redelivery of the same message
        conversation_id=conversation_id,
        # A reply is addressed to (provider, chat) and `OutboundMessage` carries only `contact_id`,
        # so in a private chat the chat IS the contact (sender.user_id may differ from chat_id).
        contact_id=chat_id,
        session_id=conversation_id,
        text=text,
        occurred_at=_timestamp(dispatch.get("timestamp")) or received_at,
        received_at=received_at,
        provider_message_id=message_id,
    )
