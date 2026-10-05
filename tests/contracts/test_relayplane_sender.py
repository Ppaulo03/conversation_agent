"""RelayPlaneSender against the gateway simulator (real HTTP), following the gateway's published
contract: asynchronous sends with Idempotency-Key replay, status by `GET /messages/{id}`,
`resolve`, `limits`. The assumptions that carry safety are also checked, opt-in, against a
deployed gateway (`test_relayplane_live.py`)."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError

from conftest import RELAY_RETENTION, RELAY_T0, RelayHandle
from conversation_agent.adapters.connections.static import StaticConnectionResolver
from conversation_agent.adapters.secrets.providers import InMemorySecretProvider
from conversation_agent.adapters.senders.relayplane import RelayPlaneSender, result_from_gateway
from conversation_agent.core.models.connections import AuthSpec, ResolvedConnection
from conversation_agent.core.models.delivery import DeliveryPolicy
from conversation_agent.core.models.runtime import OutboundMessage, OutboxStatus

TENANT = "tenant-1"


def message(key: str = "key-1", text: str = "Olá!") -> OutboundMessage:
    return OutboundMessage(
        outbox_id=f"ob-{key}",
        tenant_id=TENANT,
        conversation_id="inst_1:5511999990000",
        channel_id="inst_1",
        contact_id="5511999990000",
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


async def test_a_send_is_queued_with_the_gateways_id_and_the_documented_request(
    relay: RelayHandle,
) -> None:
    result = await sender_for(relay).send(message())
    assert result.status is OutboxStatus.QUEUED  # accepted DURABLY, not yet by the provider
    assert result.channel_message_id == "msg_1"
    assert result.provider_message_id is None and result.provider_accepted_at is None
    (sent,) = relay.sent
    assert sent["instance_id"] == "inst_1" and sent["to"] == "5511999990000"
    assert sent["type"] == "text" and sent["payload"] == {"text": "Olá!"}
    assert relay.state.requests[-1]["headers"]["idempotency-key"] == "key-1"


async def test_the_same_row_sent_twice_is_one_message_not_two(relay: RelayHandle) -> None:
    sender = sender_for(relay)
    first = await sender.send(message())
    again = await sender.send(message())  # a technical retry of the SAME row (same key, same body)
    assert again.channel_message_id == first.channel_message_id and len(relay.sent) == 1


async def test_the_provider_id_and_the_channels_acceptance_time_arrive_with_accepted(
    relay: RelayHandle,
) -> None:
    sender = sender_for(relay)
    first = await sender.send(message())
    relay.set_time(RELAY_T0 + timedelta(hours=3))  # the channel's clock is not ours (INV-026)
    relay.settle("msg_1", "ACCEPTED", provider_message_id="3EB076A9503CA689E453E4")
    row = message().model_copy(update={"channel_message_id": first.channel_message_id})
    found = await sender.lookup(row)
    assert found.status is OutboxStatus.ACCEPTED
    assert found.provider_message_id == "3EB076A9503CA689E453E4"  # what a quoted reply carries
    assert found.provider_accepted_at == RELAY_T0 + timedelta(hours=3)


@pytest.mark.parametrize(
    ("gateway", "expected"),
    [
        ("QUEUED", OutboxStatus.QUEUED),
        ("DISPATCHING", OutboxStatus.QUEUED),
        ("ACCEPTED", OutboxStatus.ACCEPTED),
        ("DELIVERED", OutboxStatus.ACCEPTED),
        ("READ", OutboxStatus.ACCEPTED),
        ("FAILED", OutboxStatus.FAILED),
        ("UNKNOWN", OutboxStatus.UNKNOWN),
    ],
)
def test_every_gateway_status_maps_to_the_outbox_taxonomy(
    gateway: str, expected: OutboxStatus
) -> None:
    result = result_from_gateway(gateway, channel_message_id="m1", provider_message_id="p1")
    assert result is not None and result.status is expected
    assert result_from_gateway("TELEPORTED", channel_message_id="m1") is None  # never assumed


@pytest.mark.parametrize(
    ("fault", "status", "retryable"),
    [
        ({"status": 400}, OutboxStatus.FAILED, False),
        ({"status": 404}, OutboxStatus.FAILED, False),
        ({"status": 409}, OutboxStatus.FAILED, False),
        ({"status": 413}, OutboxStatus.FAILED, False),
        ({"status": 422}, OutboxStatus.FAILED, False),
        ({"status": 429}, OutboxStatus.FAILED, True),
        ({"status": 500}, OutboxStatus.UNKNOWN, False),
        ({"status": 503}, OutboxStatus.UNKNOWN, False),
    ],
)
async def test_http_outcomes_map_to_the_outbox_taxonomy(
    relay: RelayHandle, fault: dict[str, int], status: OutboxStatus, retryable: bool
) -> None:
    relay.state.fault = fault
    result = await sender_for(relay).send(message())
    assert (result.status, result.retryable) == (status, retryable)


async def test_a_lost_response_is_unknown_and_resending_the_same_key_is_a_replay(
    relay: RelayHandle,
) -> None:
    relay.state.fault = {"status_after_effect": 503}  # the gateway DID take the message
    sender = sender_for(relay)
    lost = await sender.send(message())
    assert lost.status is OutboxStatus.UNKNOWN and lost.channel_message_id is None
    assert len(relay.sent) == 1
    relay.state.fault = None
    again = await sender.send(message())  # safe while the gateway remembers the key
    assert again.status is OutboxStatus.QUEUED and again.channel_message_id == "msg_1"
    assert len(relay.sent) == 1  # a replay, never a duplicate


async def test_a_timeout_is_unknown_never_failed(relay: RelayHandle) -> None:
    relay.state.fault = {"delay": 1.0}
    result = await sender_for(relay, max_timeout_seconds=0.2).send(message())
    assert result.status is OutboxStatus.UNKNOWN


async def test_after_its_retention_the_gateway_forgets_the_key_and_a_resend_duplicates(
    relay: RelayHandle,
) -> None:
    sender = sender_for(relay)
    await sender.send(message())
    relay.advance(RELAY_RETENTION + timedelta(minutes=5))
    await sender.send(message())  # same key, but forgotten: a NEW message
    assert len(relay.sent) == 2  # which is exactly why the retry horizon exists


async def test_the_same_key_with_another_payload_is_refused_by_the_gateway(
    relay: RelayHandle,
) -> None:
    sender = sender_for(relay)
    await sender.send(message(text="primeira"))
    clash = await sender.send(message(text="outra coisa"))  # a bug on our side: 422
    assert (clash.status, clash.retryable) == (OutboxStatus.FAILED, False)
    assert len(relay.sent) == 1


async def test_lookup_needs_the_gateways_id_and_a_readable_answer(relay: RelayHandle) -> None:
    sender = sender_for(relay)
    with pytest.raises(RuntimeError, match="never gave an id"):
        await sender.lookup(message())
    unknown_id = message().model_copy(update={"channel_message_id": "msg_404"})
    with pytest.raises(RuntimeError, match="404"):
        await sender.lookup(unknown_id)  # an error proves nothing


async def test_a_gateway_unknown_is_reported_unknown_and_only_an_operator_resolves_it(
    relay: RelayHandle,
) -> None:
    sender = sender_for(relay)
    first = await sender.send(message())
    relay.settle("msg_1", "UNKNOWN")
    row = message().model_copy(update={"channel_message_id": first.channel_message_id})
    assert (await sender.lookup(row)).status is OutboxStatus.UNKNOWN
    resolved = await sender.resolve(row, sent=True)
    assert resolved.status is OutboxStatus.ACCEPTED
    with pytest.raises(RuntimeError):
        await sender.resolve(row, sent=False)  # no longer UNKNOWN: the gateway refuses (409)


async def test_the_delivery_policy_is_checked_against_what_the_gateway_reports(
    relay: RelayHandle,
) -> None:
    sender = sender_for(relay)
    policy = await sender.delivery_policy(TENANT, retry_horizon=timedelta(hours=2))
    assert policy.idempotency_retention == RELAY_RETENTION
    with pytest.raises(ValidationError, match="sender_retry_horizon"):
        await sender.delivery_policy(TENANT, retry_horizon=RELAY_RETENTION + timedelta(hours=1))
    relay.state.fault = {"status": 500}
    with pytest.raises(RuntimeError):
        await sender.delivery_policy(TENANT, retry_horizon=timedelta(hours=1))


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
    result = await RelayPlaneSender(StaticConnectionResolver({})).send(message())
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


async def test_an_unreadable_or_unexpected_answer_is_unknown() -> None:
    sender = RelayPlaneSender(StaticConnectionResolver({}))
    odd = httpx.Response(202, json={"message_id": "m", "status": "TELEPORTED"})
    assert sender._interpret(odd).status is OutboxStatus.UNKNOWN
    no_id = httpx.Response(202, json={"status": "QUEUED"})
    assert sender._interpret(no_id).status is OutboxStatus.UNKNOWN
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


def test_a_negative_reconcile_margin_cannot_extend_the_idempotency_window() -> None:
    with pytest.raises(ValidationError, match="reconcile_margin"):
        DeliveryPolicy(
            idempotency_retention=timedelta(minutes=10),
            retry_horizon=timedelta(minutes=10),
            reconcile_margin=timedelta(minutes=-5),  # used to make 'safe to resend' last 15 min
        )
    with pytest.raises(ValidationError, match="reconcile_margin"):
        DeliveryPolicy(  # a margin as long as the retention leaves no safe window at all
            idempotency_retention=timedelta(minutes=10),
            retry_horizon=timedelta(minutes=5),
            reconcile_margin=timedelta(minutes=10),
        )
    ok = DeliveryPolicy(
        idempotency_retention=timedelta(minutes=10),
        retry_horizon=timedelta(minutes=5),
        reconcile_margin=timedelta(0),
    )
    assert ok.safe_resend_until == timedelta(minutes=10)  # never beyond the retention
