"""Contract test against a DEPLOYED gateway (docs/RELAYPLANE_CONTRACT.md). Skipped unless the
RELAYPLANE_* variables are set: it sends real messages, so it is opt-in.

It checks what the framework's safety rests on: Idempotency-Key replay (the same key twice is one
message), the idempotency retention the gateway reports (`GET /limits`) against the configured
retry horizon and, when asked to wait, that the gateway really remembers a key that long.
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
from conversation_agent.core.models.runtime import OutboundMessage, OutboxStatus

pytestmark = pytest.mark.integration

_REQUIRED = (
    "RELAYPLANE_BASE_URL",
    "RELAYPLANE_TOKEN",
    "RELAYPLANE_CHANNEL_ID",
    "RELAYPLANE_TO",
    "RELAYPLANE_RETRY_HORIZON_SECONDS",
)
_MISSING = [name for name in _REQUIRED if not os.environ.get(name)]
needs_gateway = pytest.mark.skipif(bool(_MISSING), reason=f"set {', '.join(_MISSING)}")


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


def message(key: str, channel_message_id: str | None = None) -> OutboundMessage:
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
        channel_message_id=channel_message_id,
    )


@needs_gateway
async def test_the_configured_retry_horizon_fits_what_the_gateway_reports() -> None:
    horizon = timedelta(seconds=float(os.environ["RELAYPLANE_RETRY_HORIZON_SECONDS"]))
    policy = await sender().delivery_policy("live", retry_horizon=horizon)  # raises if it exceeds
    assert policy.idempotency_retention >= horizon


@needs_gateway
async def test_the_same_key_twice_is_one_message_not_two() -> None:
    key = f"contract-{uuid.uuid4()}"
    first = await sender().send(message(key))
    assert first.status is OutboxStatus.QUEUED and first.channel_message_id
    again = await sender().send(message(key))  # a replay: the safe reconciliation primitive
    assert again.channel_message_id == first.channel_message_id
    found = await sender().lookup(message(key, first.channel_message_id))
    assert found.status in (OutboxStatus.QUEUED, OutboxStatus.ACCEPTED, OutboxStatus.UNKNOWN)


@needs_gateway
@pytest.mark.skipif(os.environ.get("RELAYPLANE_MEASURE_RETENTION") != "1", reason="slow: opt in")
async def test_the_gateway_remembers_the_key_for_the_whole_claimed_retention() -> None:
    horizon = timedelta(seconds=float(os.environ["RELAYPLANE_RETRY_HORIZON_SECONDS"]))
    claimed = (await sender().delivery_policy("live", retry_horizon=horizon)).idempotency_retention
    key = f"contract-retention-{uuid.uuid4()}"
    first = await sender().send(message(key))
    await asyncio.sleep(claimed.total_seconds() - 5)  # still inside the claimed window
    again = await sender().send(message(key))
    assert again.channel_message_id == first.channel_message_id  # replayed, not duplicated
