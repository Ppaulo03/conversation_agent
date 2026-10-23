"""MessageSender over the Wyrm core's output path (docs/WYRM_CHANNEL.md).

Sending is a SYNCHRONOUS publish (pika) that returns once the broker confirmed it:

    publisher.send_message(provider_id, chat_id, text=..., event_id=<outbox idempotency_key>)
        returns                       -> ACCEPTED (the broker has it; no receipt, no provider clock)
        ValueError (InvalidMessage)   -> FAILED, not retryable (nothing was published)
        any other error               -> escapes: the framework records UNKNOWN (the broker may or
                                         may not have the message)

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
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import partial
from typing import Protocol

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


class WyrmSender:
    def __init__(self, publisher: WyrmPublish) -> None:
        self._publisher = publisher
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wyrm-publish")

    def close(self) -> None:
        self._executor.shutdown(wait=True)

    @staticmethod
    def delivery_policy() -> DeliveryPolicy:
        return DeliveryPolicy.no_retry(idempotency_retention=_RETENTION)

    async def send(self, message: OutboundMessage) -> SendResult:
        try:
            await asyncio.get_running_loop().run_in_executor(
                self._executor,
                partial(
                    self._publisher.send_message,
                    message.channel_id,  # the Wyrm provider_id
                    message.contact_id,  # the Wyrm chat_id (see inbound.py)
                    message.text,
                    event_id=message.idempotency_key,  # stable per delivery (not a dedupe key here)
                ),
            )
        except ValueError:
            return SendResult(status=OutboxStatus.FAILED, retryable=False)  # refused before I/O
        return SendResult(status=OutboxStatus.ACCEPTED)  # provider_accepted_at stays None
