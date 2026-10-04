from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict


class Subscription(BaseModel):
    """What the OPERATOR registered for one webhook endpoint: who owns it and how it is signed.
    Our tenant comes from here, never from the payload (a payload cannot choose it)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subscription_id: str
    tenant_id: str  # OUR tenant
    secret_ref: str  # the subscription secret (`whsec_...`), held by the SecretProvider
    relay_tenant_id: str | None = None  # when set, the envelope's tenant must be this one
    instance_ids: tuple[str, ...] = ()  # empty = any instance of that gateway tenant


class SubscriptionResolver(Protocol):
    async def resolve(self, subscription_id: str) -> Subscription | None: ...
