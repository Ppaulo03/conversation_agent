"""INV-020/074: what an inbound adapter must hand to `Runtime.receive` (ADR-025)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from conversation_agent.core.models.runtime import InboundEvent

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def build(**overrides: object) -> InboundEvent:
    fields: dict[str, object] = {
        "tenant_id": "t",
        "channel_id": "ch",
        "event_id": "ev-1",
        "conversation_id": "ch:5511",
        "contact_id": "5511",
        "session_id": "ch:5511",
        "text": "oi",
        "occurred_at": NOW,
        "received_at": NOW,
    }
    return InboundEvent(**{**fields, **overrides})  # type: ignore[arg-type]


def test_an_event_without_an_identity_is_refused_when_it_is_built() -> None:
    with pytest.raises(ValidationError):
        build(event_id="")


def test_an_ordinary_event_is_valid() -> None:
    event = build()
    assert (event.event_id, event.kind, event.source_sequence) == ("ev-1", "user", None)
    assert event.provider_occurred_at is None and event.reply_to_provider_message_id is None


def test_the_runtimes_own_system_event_ids_stay_valid() -> None:
    assert build(event_id="system:proactive:42", kind="system").kind == "system"


def test_timestamps_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError):
        build(occurred_at=datetime(2026, 10, 8, 12, 0))
