"""The generic delivery policy and the SendResult semantics it relies on (INV-072, ADR-023)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from conversation_agent.adapters.senders.console import CONSOLE_DELIVERY_POLICY
from conversation_agent.core.models.delivery import DeliveryPolicy
from conversation_agent.core.models.runtime import OutboxStatus, SendResult

HOUR = timedelta(hours=1)


def test_the_safe_resend_window_is_the_retention_minus_the_safety_margin() -> None:
    policy = DeliveryPolicy(
        idempotency_retention=HOUR,
        retry_horizon=timedelta(minutes=30),
        safety_margin=timedelta(minutes=5),
    )
    assert policy.safe_resend_window == timedelta(minutes=55)


def test_the_retry_horizon_may_fill_the_safe_window_but_not_the_retention() -> None:
    margin = timedelta(minutes=5)
    DeliveryPolicy(idempotency_retention=HOUR, retry_horizon=HOUR - margin, safety_margin=margin)
    with pytest.raises(ValidationError, match="retry_horizon must be"):
        DeliveryPolicy(  # fits the retention, not the retention minus the margin
            idempotency_retention=HOUR, retry_horizon=HOUR, safety_margin=margin
        )


def test_zero_retry_is_valid_and_explicit() -> None:
    policy = DeliveryPolicy(idempotency_retention=HOUR, retry_horizon=timedelta(0))
    assert not policy.retries_enabled
    assert DeliveryPolicy.no_retry(HOUR).retry_horizon == timedelta(0)
    assert DeliveryPolicy(
        idempotency_retention=HOUR, retry_horizon=timedelta(seconds=1)
    ).retries_enabled
    with pytest.raises(ValidationError, match="retry_horizon"):
        DeliveryPolicy(idempotency_retention=HOUR, retry_horizon=timedelta(seconds=-1))


@pytest.mark.parametrize(
    ("retention", "margin"),
    [
        (timedelta(0), timedelta(0)),  # no retention at all
        (timedelta(minutes=-1), timedelta(0)),
        (HOUR, timedelta(minutes=-5)),  # would push "safe to resend" past the retention
        (HOUR, HOUR),  # a margin as long as the retention leaves no window
        (HOUR, HOUR + timedelta(minutes=1)),
    ],
)
def test_an_invalid_retention_and_margin_are_refused(
    retention: timedelta, margin: timedelta
) -> None:
    with pytest.raises(ValidationError):
        DeliveryPolicy(
            idempotency_retention=retention, retry_horizon=timedelta(0), safety_margin=margin
        )


def test_a_policy_has_no_unbounded_form() -> None:
    with pytest.raises(ValidationError):
        DeliveryPolicy(idempotency_retention=HOUR)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        DeliveryPolicy(idempotency_retention=HOUR, retry_horizon=None)  # type: ignore[arg-type]


def test_the_console_channel_declares_an_explicit_zero_retry_policy() -> None:
    assert CONSOLE_DELIVERY_POLICY.retry_horizon == timedelta(0)
    assert not CONSOLE_DELIVERY_POLICY.retries_enabled


def test_a_queued_result_must_carry_the_handle_it_is_settled_by() -> None:
    with pytest.raises(ValidationError, match="channel_message_id"):
        SendResult(status=OutboxStatus.QUEUED)
    with pytest.raises(ValidationError, match="channel_message_id"):
        SendResult(status=OutboxStatus.QUEUED, channel_message_id="")
    assert (
        SendResult(status=OutboxStatus.QUEUED, channel_message_id="h1").channel_message_id == "h1"
    )
    # the other outcomes do not need one
    for status in (OutboxStatus.ACCEPTED, OutboxStatus.FAILED, OutboxStatus.UNKNOWN):
        assert SendResult(status=status).channel_message_id is None
