"""The Wyrm channel adapter: dispatch mapping and sender (docs/WYRM_CHANNEL.md)."""

from __future__ import annotations

import asyncio
import copy
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from conversation_agent.adapters.clock import FixedClock
from conversation_agent.adapters.wyrm import WyrmSender, conversation_id_for, dispatch_to_event
from conversation_agent.core.models.runtime import OutboundMessage, OutboxStatus
from conversation_agent.ports.channel_lookup import MessageLookup
from conversation_agent.ports.sender import MessageSender

TENANT = "tenant-1"
NOW = datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC)

DISPATCH: dict[str, Any] = {
    "dispatch_id": "dsp-1",
    "timestamp": "2026-01-01T12:00:01Z",
    "context": {
        "tenant_id": TENANT,
        "provider_id": "telegram-main",
        "chat_id": "chat-42",
        "session_key": "tenant-1:telegram-main:chat-42",
        "channel_type": "PRIVATE",
    },
    "sender": {"user_id": "user-7", "username": "jdoe"},
    "payload": {"message_id": "msg-1", "text": "oi", "attachments": []},
}


def dispatch(**changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(DISPATCH)
    for path, value in changes.items():
        section, _, key = path.partition("__")
        if key:
            data[section][key] = value
        else:
            data[section] = value
    return data


def map_it(data: dict[str, Any]) -> Any:
    return dispatch_to_event(data, tenant_id=TENANT, clock=FixedClock(NOW))


def test_a_private_text_dispatch_maps_to_an_event_without_provider_evidence() -> None:
    # No payload.provider_timestamp in the base dispatch, so no provider evidence.
    event = map_it(dispatch())
    assert event is not None
    assert (event.tenant_id, event.channel_id, event.contact_id) == (
        TENANT,
        "telegram-main",
        "chat-42",
    )
    assert event.event_id == "telegram-main:chat-42:msg-1"
    assert event.provider_message_id == "msg-1"
    assert event.text == "oi"
    assert event.occurred_at == datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC)
    assert event.received_at == NOW
    assert event.provider_occurred_at is None  # the core's clock is not provider evidence (INV-026)
    assert event.reply_to_provider_message_id is None
    assert event.kind == "user"


def test_the_provider_timestamp_is_the_platform_clock_not_the_cores() -> None:
    event = map_it(dispatch(payload__provider_timestamp="2026-01-01T12:00:00Z"))
    assert event is not None
    assert event.provider_occurred_at == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert event.occurred_at == datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC)  # the core's, apart


@pytest.mark.parametrize("value", [None, 1767268800, "yesterday", "2026-01-01T12:00:00", ""])
def test_a_missing_or_unusable_provider_timestamp_is_no_evidence(value: object) -> None:
    # A zone-less value is not guessed (INV-026); the event is still mapped.
    event = map_it(dispatch(payload__provider_timestamp=value))
    assert event is not None
    assert event.provider_occurred_at is None


def test_a_provider_timestamp_with_an_offset_is_kept_as_an_instant() -> None:
    event = map_it(dispatch(payload__provider_timestamp="2026-01-01T09:00:00-03:00"))
    assert event is not None
    assert event.provider_occurred_at == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def test_without_a_message_id_the_dispatch_is_not_mapped() -> None:
    data = dispatch()
    del data["payload"]["message_id"]
    assert map_it(data) is None  # dispatch_id changes on a redelivery: never an event identity


def test_a_non_string_message_id_is_not_mapped() -> None:
    assert map_it(dispatch(payload={"message_id": 42, "text": "oi"})) is None


def test_a_missing_channel_type_is_not_mapped() -> None:
    data = dispatch()
    del data["context"]["channel_type"]
    assert map_it(data) is None


def test_an_unparseable_timestamp_falls_back_to_the_receive_time() -> None:
    event = map_it(dispatch(timestamp="not a date"))
    assert event is not None
    assert event.occurred_at == event.received_at == NOW


def test_a_redelivered_dispatch_maps_to_the_same_event_id_and_conversation() -> None:
    first = dispatch_to_event(dispatch(), tenant_id=TENANT, clock=FixedClock(NOW))
    again = dispatch_to_event(
        dispatch(dispatch_id="dsp-2"),  # another delivery of the same message
        tenant_id=TENANT,
        clock=FixedClock(NOW + timedelta(minutes=3)),
    )
    assert first is not None and again is not None
    assert first.event_id == again.event_id
    assert first.conversation_id == again.conversation_id


def test_the_conversation_id_is_deterministic_namespaced_and_per_chat() -> None:
    event = map_it(dispatch())
    assert event is not None
    assert event.conversation_id == conversation_id_for("telegram-main", "chat-42")
    assert event.conversation_id == "wyrm:telegram-main:chat-42"
    other = map_it(dispatch(context__chat_id="chat-43"))
    assert other is not None and other.conversation_id != event.conversation_id


def test_a_missing_timestamp_falls_back_to_the_receive_time() -> None:
    data = dispatch()
    del data["timestamp"]
    event = map_it(data)
    assert event is not None
    assert event.occurred_at == event.received_at == NOW


def test_a_group_dispatch_is_not_mapped() -> None:
    assert map_it(dispatch(context__channel_type="GROUP")) is None


@pytest.mark.parametrize(
    "data",
    [
        dispatch(payload__text=""),
        dispatch(payload={"message_id": "m", "attachments": [{"id": "a"}]}),  # media only
        dispatch(context__chat_id="a:b"),  # an address we could not answer
        dispatch(context__provider_id=""),
        {"dispatch_id": "dsp-1"},
    ],
)
def test_a_dispatch_with_no_text_or_no_usable_address_is_not_mapped(data: dict[str, Any]) -> None:
    assert map_it(data) is None


def test_a_dispatch_for_another_tenant_is_refused() -> None:
    with pytest.raises(ValueError, match="another tenant"):
        map_it(dispatch(context__tenant_id="tenant-2"))


# ---------------------------------------------------------------------------------------- sender


class FakePublisher:
    def __init__(
        self, error: Exception | None = None, receipt: dict[str, Any] | None = None
    ) -> None:
        self.calls: list[tuple[str, str, str | None, str | None]] = []
        self.waited: list[tuple[str, str, str | None, str | None, float]] = []
        self.error = error
        self.receipt = receipt

    def send_message_and_wait_receipt(
        self,
        provider_id: str,
        chat_id: str,
        text: str | None = None,
        *,
        event_id: str | None = None,
        wait_seconds: float = 0.0,
    ) -> dict[str, Any] | None:
        if self.error is not None:
            raise self.error
        self.waited.append((provider_id, chat_id, text, event_id, wait_seconds))
        return self.receipt

    def send_message(
        self,
        provider_id: str,
        chat_id: str,
        text: str | None = None,
        *,
        event_id: str | None = None,
    ) -> str:
        if self.error is not None:
            raise self.error
        self.calls.append((provider_id, chat_id, text, event_id))
        return event_id or ""


def outbound(key: str = "key-1", action_id: str | None = None) -> OutboundMessage:
    return OutboundMessage(
        outbox_id="ob-1",
        tenant_id=TENANT,
        conversation_id="wyrm:telegram-main:chat-42",
        channel_id="telegram-main",
        contact_id="chat-42",
        turn_id="t1",
        message_index=0,
        text="Olá!",
        idempotency_key=key,
        action_id=action_id,
    )


async def test_a_published_message_is_accepted_without_a_provider_clock() -> None:
    publisher = FakePublisher()
    result = await WyrmSender(publisher).send(outbound())
    assert result.status is OutboxStatus.ACCEPTED
    assert result.provider_accepted_at is None  # INV-026: no comparable timestamp
    assert result.provider_message_id is None


async def test_the_outbox_key_is_the_wyrm_event_id_and_the_address_is_provider_and_chat() -> None:
    publisher = FakePublisher()
    await WyrmSender(publisher).send(outbound("key-9"))
    assert publisher.calls == [("telegram-main", "chat-42", "Olá!", "key-9")]


async def test_a_message_the_sdk_refuses_is_failed_and_not_retryable() -> None:
    result = await WyrmSender(FakePublisher(ValueError("bad chat"))).send(outbound())
    assert result.status is OutboxStatus.FAILED
    assert result.retryable is False


async def test_a_broker_error_escapes_so_the_framework_records_unknown() -> None:
    with pytest.raises(ConnectionError):
        await WyrmSender(FakePublisher(ConnectionError("broker down"))).send(outbound())


def test_the_sender_and_its_policy_fit_the_runtime_contract() -> None:
    # `Runtime.build` needs Postgres; what it asks of a channel is checked here: a policy that
    # validates, no resend, and no lookup (an UNKNOWN row has no settlement path over Wyrm).
    sender = WyrmSender(FakePublisher())
    checked: MessageSender = sender  # structural: mypy proves it
    assert checked is sender
    assert not isinstance(sender, MessageLookup)
    policy = sender.delivery_policy()
    assert policy.safe_resend_window == timedelta(minutes=59)
    assert policy.retry_horizon <= policy.safe_resend_window


async def test_publishes_run_one_at_a_time_on_one_thread() -> None:
    threads: set[int] = set()
    running = 0
    overlap = False

    class Probe(FakePublisher):
        def send_message(self, *args: Any, **kwargs: Any) -> str:
            nonlocal running, overlap
            running += 1
            overlap = overlap or running > 1
            threads.add(threading.get_ident())
            time.sleep(0.01)
            running -= 1
            return ""

    sender = WyrmSender(Probe())
    await asyncio.gather(*(sender.send(outbound(f"k{i}")) for i in range(5)))
    sender.close()
    assert len(threads) == 1 and not overlap


def test_the_delivery_policy_never_resends() -> None:
    policy = WyrmSender.delivery_policy()
    assert policy.retry_horizon == timedelta(0)
    assert not policy.retries_enabled


# ------------------------------------------------------------------- confirmation prompts (receipts)

RECEIPT = {
    "output_event_id": "key-1",
    "provider_id": "telegram-main",
    "status": "ACCEPTED",
    "provider_message_id": "wamid-1",
    "provider_timestamp": "2026-01-01T12:00:03Z",
}


async def test_a_confirmation_prompt_waits_for_the_receipt_and_carries_the_platform_clock() -> None:
    publisher = FakePublisher(receipt=RECEIPT)
    result = await WyrmSender(publisher, receipt_wait_seconds=7).send(outbound(action_id="act-1"))
    assert result.status is OutboxStatus.ACCEPTED
    assert result.provider_message_id == "wamid-1"
    assert result.provider_accepted_at == datetime(2026, 1, 1, 12, 0, 3, tzinfo=UTC)
    assert publisher.waited == [("telegram-main", "chat-42", "Olá!", "key-1", 7)]
    assert publisher.calls == []  # it did not publish a second time


async def test_an_ordinary_message_does_not_wait_for_a_receipt() -> None:
    publisher = FakePublisher(receipt=RECEIPT)
    result = await WyrmSender(publisher).send(outbound())
    assert result.status is OutboxStatus.ACCEPTED and result.provider_accepted_at is None
    assert publisher.waited == [] and len(publisher.calls) == 1


async def test_no_receipt_in_time_is_accepted_without_a_provider_clock() -> None:
    # The broker has it, so it may be delivered; nothing can prove the reply came after it.
    result = await WyrmSender(FakePublisher(receipt=None)).send(outbound(action_id="act-1"))
    assert result.status is OutboxStatus.ACCEPTED
    assert result.provider_accepted_at is None and result.provider_message_id is None


async def test_a_rejected_receipt_is_failed_and_not_retryable() -> None:
    receipt = {**RECEIPT, "status": "REJECTED", "reason": "blocked"}
    result = await WyrmSender(FakePublisher(receipt=receipt)).send(outbound(action_id="act-1"))
    assert result.status is OutboxStatus.FAILED
    assert result.retryable is False


@pytest.mark.parametrize("stamp", [None, "yesterday", "2026-01-01T12:00:03", 1767268803])
async def test_a_receipt_without_a_usable_platform_clock_proves_no_ordering(stamp: object) -> None:
    receipt = {**RECEIPT, "provider_timestamp": stamp}
    result = await WyrmSender(FakePublisher(receipt=receipt)).send(outbound(action_id="act-1"))
    assert result.status is OutboxStatus.ACCEPTED
    assert result.provider_accepted_at is None  # a zone-less value is not guessed (INV-026)
    assert result.provider_message_id == "wamid-1"


async def test_a_prompt_the_sdk_refuses_is_failed_and_a_broker_error_escapes() -> None:
    refused = await WyrmSender(FakePublisher(ValueError("bad chat"))).send(outbound(action_id="a"))
    assert refused.status is OutboxStatus.FAILED and refused.retryable is False
    with pytest.raises(ConnectionError):
        await WyrmSender(FakePublisher(ConnectionError("down"))).send(outbound(action_id="a"))
