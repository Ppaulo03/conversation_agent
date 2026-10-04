from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict


class Subscription(BaseModel):
    """What the OPERATOR registered for one webhook endpoint: who owns it and how it is signed.
    Tenant and channel come from here, never from the payload (a payload cannot choose them)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subscription_id: str
    tenant_id: str
    channel_id: str
    secret_ref: str


class SubscriptionResolver(Protocol):
    async def resolve(self, subscription_id: str) -> Subscription | None: ...
