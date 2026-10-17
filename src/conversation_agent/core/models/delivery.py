"""Outbound delivery policy (INV-034, INV-072, RUNTIME_PROTOCOL 8, ADR-023).

A channel dedupes on the idempotency key only for a limited time (its idempotency retention). A
resend that reuses the key is safe only while the channel still remembers it, so the runtime must
never send again outside a window that closes BEFORE that memory fades:

    safe_resend_window = idempotency_retention - safety_margin
    0 <= retry_horizon <= safe_resend_window

Two distinct time concepts:
  - `retry_horizon` bounds ordinary, direct retries: a retryable proven FAILED and the direct
    recovery of a stale SENDING. It is NOT the latest instant a delivery can be resent.
    `retry_horizon == 0` disables every resend; a positive value also enables ambiguity recovery.
  - `safe_resend_window` is the absolute outer envelope. An UNKNOWN without a handle may still get
    a reconciler-authorised same-key resend AFTER `retry_horizon`, until the window closes.
No resend, however authorised, leaves `safe_resend_window`. `retry_horizon == 0` is the explicit
"never send the same delivery again": no tiny positive value stands in for it.
Every Runtime has exactly one policy and it must hold for every delivery the Runtime
handles: heterogeneous channels use a conservative policy or separate runtimes.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel, ConfigDict, model_validator


class DeliveryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    idempotency_retention: timedelta  # how long the channel remembers an idempotency key
    # ordinary/direct retries only (0: never resend); ambiguity recovery may go on until the window
    retry_horizon: timedelta
    safety_margin: timedelta = timedelta(minutes=1)  # gap kept inside the retention window

    @model_validator(mode="after")
    def _horizon_fits_the_safe_window(self) -> DeliveryPolicy:
        if self.idempotency_retention <= timedelta(0):
            raise ValueError("idempotency_retention must be positive")
        if not timedelta(0) <= self.safety_margin < self.idempotency_retention:
            # a negative margin would push the "safe to resend" instant PAST the moment the channel
            # forgets the key (retention - (-5 min) = retention + 5 min): a duplicate in waiting
            raise ValueError(
                "safety_margin must be >= 0 and shorter than the idempotency_retention"
            )
        if self.retry_horizon < timedelta(0):
            raise ValueError("retry_horizon must be >= 0 (0 means: never send again)")
        if self.retry_horizon > self.safe_resend_window:
            raise ValueError(
                "retry_horizon must be <= idempotency_retention - safety_margin: after the "
                "retention the channel may have forgotten the key and a resend could duplicate"
            )
        return self

    @classmethod
    def no_retry(
        cls, idempotency_retention: timedelta, safety_margin: timedelta = timedelta(minutes=1)
    ) -> DeliveryPolicy:
        """Explicit zero-retry policy: the first attempt is the only one."""
        return cls(
            idempotency_retention=idempotency_retention,
            retry_horizon=timedelta(0),
            safety_margin=safety_margin,
        )

    @property
    def safe_resend_window(self) -> timedelta:
        """Age of a delivery (since its first send) beyond which NO resend is ever allowed."""
        return self.idempotency_retention - self.safety_margin

    @property
    def retries_enabled(self) -> bool:
        return self.retry_horizon > timedelta(0)
