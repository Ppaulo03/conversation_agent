# reference-relayplane

A **simulator** of the RelayPlane gateway (`github.com/Ppaulo03/RelayPlane`), following its published
contract (`docs/CONTRACT.md`, `docs/EVENTS.md`, `docs/openapi.yaml`; summary in
`docs/RELAYPLANE_CONTRACT.md`). It is an external system: it knows nothing about `conversation_agent`
and the framework never imports it. It exists so the Phase 6 adapters can be exercised over real HTTP.

It is NOT the real gateway. Only what the adapters use is simulated, and the assumptions that carry
safety (idempotency retention, replay) are what the opt-in live contract test checks against a
deployed gateway.

- `POST /api/v1/messages/send` (header `Idempotency-Key`): asynchronous, `202 QUEUED` + `message_id`;
  same key + same body replays (`Idempotent-Replayed: true`) for the retention window, then the key is
  forgotten (a resend creates a NEW message); same key + other body is a 422;
- `GET /api/v1/messages/{id}`, `POST /api/v1/messages/{id}/resolve`, `GET /api/v1/limits`;
- `GET /api/v1/media/{id}/content` with `ETag` = sha256;
- `webhook_headers(...)`, `envelope(...)`, `message_received(...)`: what the gateway sends to the webhook;
- `settle(app, id, status)`: test control for what the provider reported about a message.