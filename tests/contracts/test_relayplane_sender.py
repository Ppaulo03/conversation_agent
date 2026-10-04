"""RelayPlaneSender against the gateway simulator (real HTTP): the status mapping of DESIGN 40,
Idempotency-Key discipline and lookup. The assumptions here are the ones the LIVE contract test
(`test_relayplane_live.py`) verifies against a deployed gateway."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError

from conftest import RELAY_RETENTION, RELAY_T0, RelayHandle
from conversation_agent.adapters.connections.static import StaticConnectionResolver
from conversation_agent.adapters.secrets.providers import InMemorySecretProvider
from conversation_agent.adapters.senders.relayplane import RelayPlaneSender
from conversation_agent.core.models.connections import AuthSpec, ResolvedConnection
from conversation_agent.core.models.delivery import DeliveryPolicy
from conversation_agent.core.models.runtime import OutboundMessage, OutboxStatus

TENANT = "tenant-1"


def message(key: str = "key-1", text: str = "Olá!") -> OutboundMessage:
    return OutboundMessage(
        outbox_id=f"ob-{key}",
        tenant_id=TENANT,
        conversation_id="conv-1",
        channel_id="wa-1",
        contact_id="contact-1",
        turn_id="t1",
        message_index=0,
        text=text,
        idempotency_key=key,
    )


def sender_for(
    relay: RelayHandle, *, auth: AuthSpec | None = None, secrets: object = None, **conn: object
) -> RelayPlaneSender:
    connection = ResolvedConnection(
        connection_id="relayplane",
        base_url=relay.base_url,
        allow_private_networks=True,
        tls_required=False,
        auth=auth,
        **conn,  # type: ignore[arg-type]
    )
    return RelayPlaneSender(
        StaticConnectionResolver({"relayplane": connection}),
        secrets,  # type: ignore[arg-type]
    )


async def test_accepted_with_the_channels_own_clock_and_a_deduped_resend(
    relay: RelayHandle,
) -> None:
    relay.set_time(RELAY_T0 + timedelta(hours=3))  # the channel's clock is NOT ours (INV-026)
    sender = sender_for(relay)
    first = await sender.send(message())
    assert first.status is OutboxStatus.ACCEPTED and first.provider_message_id == "msg_1"
    assert first.provider_accepted_at == RELAY_T0 + timedelta(hours=3)
    again = await sender.send(message())  # a technical retry of the SAME row
    assert again.status is OutboxStatus.ACCEPTED and again.provider_message_id == "msg_1"
    assert len(relay.deliveries) == 1  # the gateway deduped on the Idempotency-Key
    assert {r["headers"]["idempotency-key"] for r in relay.state.requests} == {"key-1"}  # type: ignore[index]


async def test_queued_is_not_accepted_and_has_no_acceptance_time(relay: RelayHandle) -> None:
    relay.state.queue_mode = True
    result = await sender_for(relay).send(message())
    assert result.status is OutboxStatus.QUEUED and result.provider_accepted_at is None


@pytest.mark.parametrize(
    ("fault", "status", "retryable"),
    [
        ({"status": 400}, OutboxStatus.FAILED, False),
        ({"status": 403}, OutboxStatus.FAILED, False),
        ({"status": 429}, OutboxStatus.FAILED, True),
        ({"status": 408}, OutboxStatus.FAILED, True),
        ({"status": 500}, OutboxStatus.UNKNOWN, False),
        ({"status": 503}, OutboxStatus.UNKNOWN, False),
        ({"status": 409}, OutboxStatus.UNKNOWN, False),
    ],
)
async def test_http_outcomes_map_to_the_outbox_taxonomy(
    relay: RelayHandle, fault: dict[str, int], status: OutboxStatus, retryable: bool
) -> None:
    relay.state.fault = fault
    result = await sender_for(relay).send(message())
    assert (result.status, result.retryable) == (status, retryable)


async def test_a_lost_response_is_unknown_and_lookup_proves_what_happened(
    relay: RelayHandle,
) -> None:
    relay.state.fault = {"status_after_effect": 503}  # the gateway DID take the message
    sender = sender_for(relay)
    assert (await sender.send(message())).status is OutboxStatus.UNKNOWN
    assert len(relay.deliveries) == 1
    relay.state.fault = None
    found = await sender.lookup(message())
    assert found is not None and found.status is OutboxStatus.ACCEPTED


async def test_a_timeout_is_unknown_never_failed(relay: RelayHandle) -> None:
    relay.state.fault = {"delay": 1.0}
    result = await sender_for(relay, max_timeout_seconds=0.2).send(message())
    assert result.status is OutboxStatus.UNKNOWN


async def test_the_same_key_with_another_payload_is_never_assumed_delivered(
    relay: RelayHandle,
) -> None:
    sender = sender_for(relay)
    await sender.send(message(text="primeira"))
    clash = await sender.send(message(text="outra coisa"))  # same key, different body
    assert clash.status is OutboxStatus.UNKNOWN and len(relay.deliveries) == 1


async def test_lookup_is_none_only_when_the_gateway_has_no_such_message(
    relay: RelayHandle,
) -> None:
    sender = sender_for(relay)
    assert await sender.lookup(message("never-sent")) is None
    relay.state.fault = {"status": 500}
    with pytest.raises(RuntimeError):  # an error proves nothing
        await sender.lookup(message("never-sent"))


async def test_the_gateway_forgets_keys_after_its_retention_so_absence_is_unreliable(
    relay: RelayHandle,
) -> None:
    sender = sender_for(relay)
    await sender.send(message())
    relay.advance(RELAY_RETENTION + timedelta(minutes=5))
    assert await sender.lookup(message()) is None  # "absent" ... although it WAS delivered
    assert len(relay.deliveries) == 1  # which is why a resend beyond the window can duplicate


async def test_credentials_travel_as_auth_and_never_replace_the_idempotency_key(
    relay: RelayHandle,
) -> None:
    secrets = InMemorySecretProvider({(TENANT, "relay-token"): "tok-123"})
    sender = sender_for(relay, auth=AuthSpec(secret_ref="relay-token"), secrets=secrets)
    await sender.send(message())
    sent = relay.state.requests[-1]["headers"]
    assert sent["authorization"] == "Bearer tok-123" and sent["idempotency-key"] == "key-1"
    with pytest.raises(ValidationError):
        AuthSpec(secret_ref="x", header="Idempotency-Key")


async def test_nothing_is_sent_without_a_usable_connection_or_credential(
    relay: RelayHandle,
) -> None:
    no_connection = RelayPlaneSender(StaticConnectionResolver({}))
    result = await no_connection.send(message())
    assert (result.status, result.retryable) == (OutboxStatus.FAILED, True)
    needs_secret = sender_for(
        relay, auth=AuthSpec(secret_ref="missing"), secrets=InMemorySecretProvider({})
    )
    result = await needs_secret.send(message())
    assert (result.status, result.retryable) == (OutboxStatus.FAILED, True)
    secure = RelayPlaneSender(
        StaticConnectionResolver(
            {"relayplane": ResolvedConnection(connection_id="r", base_url="http://gw.example.com")}
        )
    )
    assert (await secure.send(message())).status is OutboxStatus.FAILED  # TLS is required
    assert relay.state.requests == []


async def test_an_unreadable_answer_is_unknown(relay: RelayHandle) -> None:
    relay.state.messages.clear()
    # the gateway answers 200/202 with a status this build does not understand: never assume
    sender = RelayPlaneSender(StaticConnectionResolver({}))
    odd = httpx.Response(202, json={"id": "m", "status": "teleported"})
    assert sender._interpret(odd).status is OutboxStatus.UNKNOWN
    garbage = httpx.Response(202, content=b"<html>")
    assert sender._interpret(garbage).status is OutboxStatus.UNKNOWN


def test_the_retry_horizon_must_fit_inside_the_gateways_idempotency_retention() -> None:
    DeliveryPolicy(idempotency_retention=timedelta(hours=1), retry_horizon=timedelta(hours=1))
    with pytest.raises(ValidationError, match="sender_retry_horizon"):
        DeliveryPolicy(
            idempotency_retention=timedelta(hours=1), retry_horizon=timedelta(hours=1, seconds=1)
        )
    with pytest.raises(ValidationError, match="positive"):
        DeliveryPolicy(idempotency_retention=timedelta(0), retry_horizon=timedelta(0))
    policy = DeliveryPolicy(
        idempotency_retention=timedelta(hours=1),
        retry_horizon=timedelta(minutes=30),
        reconcile_margin=timedelta(minutes=5),
    )
    assert policy.safe_resend_until == timedelta(minutes=55)
