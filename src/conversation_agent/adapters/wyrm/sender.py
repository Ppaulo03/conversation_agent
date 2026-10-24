"""MessageSender over the Wyrm core's output path (docs/WYRM_CHANNEL.md).

Sending is a SYNCHRONOUS publish (pika) that returns once the broker confirmed it:

    publisher.send_message(provider_id, chat_id, text=..., event_id=<outbox idempotency_key>)
        returns                       -> ACCEPTED (the broker has it; no receipt, no provider clock)
        ValueError (InvalidMessage)   -> FAILED, not retryable (nothing was published)
        any other error               -> escapes: the framework records UNKNOWN (the broker may or
                                         may not have the message)

A CONFIRMATION PROMPT (`message.action_id` set) also waits for the provider's receipt, because the
runtime accepts a "yes" only if it was written after the platform accepted the question, on the
platform's own clock (INV-022, INV-026):

    publisher.send_message_and_wait_receipt(..., wait_seconds=N)
        receipt ACCEPTED              -> ACCEPTED with provider_message_id and provider_accepted_at
        receipt REJECTED              -> FAILED, not retryable (the platform refused it)
        None (no receipt in time)     -> ACCEPTED with no provider clock: the broker has it, so the
                                         message may well be delivered, but nothing can prove the
                                         reply came after it and the runtime asks again

Ordinary messages skip the wait: they gain nothing from it and a provider that publishes no receipts
would otherwise cost `wait_seconds` per message.

The Wyrm core does NOT dedupe outputs: the same `event_id` published twice is delivered twice. The
channel therefore cannot honour the idempotency contract of `MessageSender`, and the only safe
policy is `retry_horizon == 0`: a row is sent once and an ambiguous send is never repeated.
`delivery_policy()` returns it; pass it to `Runtime.build`.

UNKNOWN has NO settlement path over Wyrm: there is no lookup (the sender is not a `MessageLookup`)
and no status push. With no handle and `retry_horizon == 0` the reconciler leaves the row UNKNOWN
for good (observable, never resent), and it holds back the NEXT message of that conversation until
its first send is older than the policy's safe resend window (retention - margin, 59 min here); then
it stops blocking. Nothing ever turns that row into ACCEPTED or FAILED.

The wyrm-sdk is NOT imported: `WyrmPublisher` satisfies `WyrmPublish` structurally. It is blocking
and pika connections are not thread-safe, so every publish runs on ONE thread owned by the sender
(serialised; `close()` stops it). A cancellation while a publish is running is ambiguous: the thread
cannot be interrupted and the message may still go out, so a cancelled or raised send is never
reported FAILED (the row ends UNKNOWN).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import partial
from typing import Any, Protocol

from conversation_agent.adapters.wyrm.inbound import parse_timestamp
from conversation_agent.core.models.delivery import DeliveryPolicy
from conversation_agent.core.models.runtime import OutboundMessage, OutboxStatus, SendResult

# Wyrm remembers no key, so this only sizes how long an unresolved send holds back the next message
# of the conversation (as in the console channel).
_RETENTION = timedelta(hours=1)


class WyrmPublish(Protocol):
    """What `wyrm_sdk.WyrmPublisher` offers. Blocking; raises ValueError for a bad message."""

    def send_message(
        self,
        provider_id: str,
        chat_id: str,
        text: str | None = None,
        *,
        event_id: str | None = None,
    ) -> str: ...

    def send_message_and_wait_receipt(
        self,
        provider_id: str,
        chat_id: str,
        text: str | None = None,
        *,
        event_id: str | None = None,
        wait_seconds: float = ...,
    ) -> Mapping[str, Any] | None: ...


class WyrmSender:
    """`receipt_wait_seconds`: how long a confirmation prompt waits for the provider receipt."""

    def __init__(self, publisher: WyrmPublish, *, receipt_wait_seconds: float = 10.0) -> None:
        self._publisher = publisher
        self._receipt_wait_seconds = receipt_wait_seconds
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wyrm-publish")

    def close(self) -> None:
        self._executor.shutdown(wait=True)

    @staticmethod
    def delivery_policy() -> DeliveryPolicy:
        return DeliveryPolicy.no_retry(idempotency_retention=_RETENTION)

    async def send(self, message: OutboundMessage) -> SendResult:
        address = (
            message.channel_id,  # the Wyrm provider_id
            message.contact_id,  # the Wyrm chat_id (see inbound.py)
            message.text,
        )
        # stable per delivery (not a dedupe key here)
        send = (
            partial(
                self._publisher.send_message_and_wait_receipt,
                *address,
                event_id=message.idempotency_key,
                wait_seconds=self._receipt_wait_seconds,
            )
            if message.action_id is not None
            else partial(self._publisher.send_message, *address, event_id=message.idempotency_key)
        )
        try:
            outcome = await asyncio.get_running_loop().run_in_executor(self._executor, send)
        except ValueError:
            return SendResult(status=OutboxStatus.FAILED, retryable=False)  # refused before I/O
        if message.action_id is None or not isinstance(outcome, Mapping):
            return SendResult(status=OutboxStatus.ACCEPTED)  # provider_accepted_at stays None
        return _from_receipt(outcome)


def _from_receipt(receipt: Mapping[str, Any]) -> SendResult:
    if receipt.get("status") == "REJECTED":
        return SendResult(status=OutboxStatus.FAILED, retryable=False)  # the platform refused it
    message_id = receipt.get("provider_message_id")
    return SendResult(
        status=OutboxStatus.ACCEPTED,
        provider_message_id=message_id if isinstance(message_id, str) and message_id else None,
        # Only the platform's own clock counts, and only with a zone (INV-026).
        provider_accepted_at=parse_timestamp(receipt.get("provider_timestamp"), require_zone=True),
    )
