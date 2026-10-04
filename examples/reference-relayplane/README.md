# reference-relayplane

A **simulator** of the channel gateway the framework talks to ("RelayPlane" in the design docs).
It is an external system: it knows nothing about `conversation_agent` and the framework never
imports it. It exists so the Phase 6 adapters can be exercised over real HTTP.

IMPORTANT: it implements the contract this project ASSUMES (see `docs/RELAYPLANE_CONTRACT.md`),
not the vendor's real API. The assumptions that matter for safety (idempotency retention, lookup
by key, webhook signature) are exactly what the live contract test checks against a deployed
gateway.

- `POST /v1/messages` (header `Idempotency-Key`) - deduped for `idempotency_retention`;
- `GET /v1/messages?idempotency_key=K` - what became of a message (404 when unknown/forgotten);
- `sign(secret, body, timestamp)` / `inbound_event(...)` - what the gateway sends to the webhook.
