"""Contract test against a DEPLOYED gateway (docs/RELAYPLANE_CONTRACT.md). Skipped unless the
RELAYPLANE_* variables are set: it sends real messages, so it is opt-in.

It checks what the framework's safety rests on: Idempotency-Key dedupe, lookup by key and, when
asked to wait, that the retention window is at least what `DeliveryPolicy` claims.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta

import pytest

from conversation_agent.adapters.connections.static import StaticConnectionResolver
from conversation_agent.adapters.secrets.providers import InMemorySecretProvider
from conversation_agent.adapters.senders.relayplane import RelayPlaneSender
from conversation_agent.core.models.connections import AuthSpec, ResolvedConnection
from conversation_agent.core.models.delivery import DeliveryPolicy
from conversation_agent.core.models.runtime import OutboundMessage, OutboxStatus

pytestmark = pytest.mark.integration

_REQUIRED = (
    "RELAYPLANE_BASE_URL",
    "RELAYPLANE_TOKEN",
    "RELAYPLANE_CHANNEL_ID",
    "RELAYPLANE_TO",
    "RELAYPLANE_IDEMPOTENCY_RETENTION_SECONDS",
    "RELAYPLANE_RETRY_HORIZON_SECONDS",
)
_MISSING = [name for name in _REQUIRED if not os.environ.get(name)]
needs_gateway = pytest.mark.skipif(bool(_MISSING), reason=f"set {', '.join(_MISSING)}")


def policy() -> DeliveryPolicy:
    return DeliveryPolicy(
        idempotency_retention=timedelta(
            seconds=float(os.environ["RELAYPLANE_IDEMPOTENCY_RETENTION_SECONDS"])
        ),
        retry_horizon=timedelta(seconds=float(os.environ["RELAYPLANE_RETRY_HORIZON_SECONDS"])),
    )


def sender() -> RelayPlaneSender:
    connection = ResolvedConnection(
        connection_id="relayplane",
        base_url=os.environ["RELAYPLANE_BASE_URL"],
        auth=AuthSpec(secret_ref="relay"),
    )
    return RelayPlaneSender(
        StaticConnectionResolver({"relayplane": connection}),
        InMemorySecretProvider({("live", "relay"): os.environ["RELAYPLANE_TOKEN"]}),
    )


def message(key: str) -> OutboundMessage:
    return OutboundMessage(
        outbox_id=key,
        tenant_id="live",
        conversation_id="live-contract",
        channel_id=os.environ["RELAYPLANE_CHANNEL_ID"],
        contact_id=os.environ["RELAYPLANE_TO"],
        turn_id="live",
        message_index=0,
        text="[contract test] pode ignorar esta mensagem",
        idempotency_key=key,
    )


@needs_gateway
def test_the_configured_retry_horizon_fits_the_configured_retention() -> None:
    policy()  # raises when sender_retry_horizon > relayplane_idempotency_retention


@needs_gateway
async def test_the_same_key_is_deduped_and_lookup_finds_it() -> None:
    key = f"contract-{uuid.uuid4()}"
    first = await sender().send(message(key))
    assert first.status in (OutboxStatus.ACCEPTED, OutboxStatus.QUEUED)
    again = await sender().send(message(key))
    assert again.provider_message_id == first.provider_message_id  # one message, not two
    found = await sender().lookup(message(key))
    assert found is not None and found.provider_message_id == first.provider_message_id
    assert await sender().lookup(message(f"never-{uuid.uuid4()}")) is None


@needs_gateway
@pytest.mark.skipif(os.environ.get("RELAYPLANE_MEASURE_RETENTION") != "1", reason="slow: opt in")
async def test_the_gateway_remembers_the_key_for_the_whole_claimed_retention() -> None:
    key = f"contract-retention-{uuid.uuid4()}"
    first = await sender().send(message(key))
    claimed = policy().idempotency_retention
    await asyncio.sleep(claimed.total_seconds() - 5)  # still inside the claimed window
    found = await sender().lookup(message(key))
    assert found is not None and found.provider_message_id == first.provider_message_id
