"""Wyrm dispatch -> InboundEvent (docs/WYRM_CHANNEL.md; docs/GUIDE.md §5).

A pure mapping: the caller (the module worker that receives the dispatch) has already authenticated
it (the Wyrm core signs what it dispatches) and passes OUR `tenant_id` from its configuration. The
identity fields come from the dispatch `context`, which the core fills, never from `payload`.

    dispatch                      InboundEvent
    payload.message_id            provider_message_id; event_id "<provider>:<chat>:<message_id>"
    context.provider_id           channel_id
    context.chat_id               contact_id (where a reply is addressed, see below)
    (provider, chat)              conversation_id  "wyrm:<provider>:<chat>" (the tenant is NOT in
                                  it: ids are unique per tenant already and `tenant_id` is passed)
    timestamp                     occurred_at (the CORE's clock; missing/invalid: receive time)
    injected clock                received_at

`event_id` rests on `payload.message_id` ALONE: a redelivery of the same message reuses it (the
`dispatch_id` is per dispatch and changes on a redelivery, so it is never a fallback). The ':' in
`provider_id` and `chat_id` is refused HERE (not by the runtime), which keeps both ids injective;
`message_id` comes last and may contain one.

`provider_occurred_at` and `reply_to_provider_message_id` stay None: the dispatch carries neither a
provider clock nor a reply-to, and the core's `timestamp` is not provider evidence (INV-026).

Returns None (nothing to hand to the runtime) for a GROUP chat (or a missing `channel_type`), for a
dispatch with no text (media only: attachments are ignored for now), with no `message_id` (no stable
event identity) and for one that does not carry the expected shape (a non-string field counts as
missing). A dispatch addressed to another tenant is a wiring bug and raises ValueError.
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
    if message_id is None:
        return None  # `dispatch_id` changes on a redelivery: without a message id we cannot dedupe
    received_at = clock.now()
    conversation_id = conversation_id_for(provider_id, chat_id)
    return InboundEvent(
        tenant_id=tenant_id,
        channel_id=provider_id,
        event_id=f"{provider_id}:{chat_id}:{message_id}",  # same on a redelivery
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
