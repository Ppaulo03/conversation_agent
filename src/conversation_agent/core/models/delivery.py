"""Outbound delivery policy (DESIGN 40, RUNTIME_PROTOCOL 8).

The channel dedupes on `Idempotency-Key` only for a limited time. A retry that reuses the key is
safe only while the channel still remembers it, so the sender must give up resending (and
reconcile or escalate) BEFORE that memory fades:

    sender_retry_horizon <= channel idempotency retention

The retention is configuration, validated here, never assumed.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel, ConfigDict, model_validator


class DeliveryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    idempotency_retention: timedelta  # how long the channel remembers an Idempotency-Key
    retry_horizon: timedelta  # how long the sender may keep re-sending the same message
    reconcile_margin: timedelta = timedelta(minutes=1)  # safety gap inside the retention window

    @model_validator(mode="after")
    def _horizon_fits_the_retention(self) -> DeliveryPolicy:
        if self.retry_horizon <= timedelta(0) or self.idempotency_retention <= timedelta(0):
            raise ValueError("retention and retry horizon must be positive")
        if not timedelta(0) <= self.reconcile_margin < self.idempotency_retention:
            # a negative margin would push the "safe to resend" instant PAST the moment the channel
            # forgets the key (retention - (-5 min) = retention + 5 min): a duplicate in waiting
            raise ValueError("reconcile_margin must be >= 0 and shorter than the retention")
        if self.retry_horizon > self.idempotency_retention:
            raise ValueError(
                "sender_retry_horizon must be <= relayplane_idempotency_retention: after the "
                "retention the channel may have forgotten the key and a resend could duplicate"
            )
        return self

    @property
    def safe_resend_until(self) -> timedelta:
        """Age of a message beyond which an absent lookup no longer proves anything."""
        return max(self.idempotency_retention - self.reconcile_margin, timedelta(0))
