# relayplane-app

A complete application that puts an agent on WhatsApp through RelayPlane. The framework has no
entrypoint of its own (ADR-022), so this is the composition an embedding application writes, small
enough to copy: `relayplane_app/app.py`.

One ASGI app:

| Path | What |
|---|---|
| `POST /webhooks/relayplane/{subscription_id}` | signed gateway events into the durable inbox (nothing is acknowledged before it is stored) |
| `GET /healthz`, `/readyz`, `/metrics` | operational endpoints; keep them off the public internet |
| background | `Runtime.run`: turns, outbox to the gateway, timers, reconciliation |

At startup it checks the schema read-only (a stale database stops the deploy instead of being
migrated by surprise) and reads the gateway's idempotency retention from `GET /api/v1/limits`, so a
resend window longer than what the gateway remembers refuses to start instead of duplicating
messages. At shutdown the turn in progress reaches a safe point before the process exits.

It runs the reference scheduling agent (`examples/vertical-slice`). For your own agent, replace
`load_compiled_agent()` and the tool connections, and replace `InMemorySecretProvider` with your
secret manager.

## Run

```bash
export DATABASE_URL=postgresql://conversation_agent:conversation_agent_dev@127.0.0.1:5432/conversation_agent
uv run python -m conversation_agent.app.migrate apply      # once per release, before the app

export RELAYPLANE_BASE_URL=https://relay.example.com
export RELAYPLANE_API_KEY=...                  # gateway API token
export RELAYPLANE_SUBSCRIPTION_ID=...          # the webhook subscription registered at the gateway
export RELAYPLANE_WEBHOOK_SECRET=whsec_...     # that subscription's signing secret
export RELAYPLANE_TENANT_ID=...                # optional: refuse events from another gateway tenant
export TENANT_ID=my-company                    # OUR tenant; payloads never choose it
export SCHEDULING_API_URL=https://api.example.com
export RETRY_HORIZON_SECONDS=3600              # must fit inside the gateway's key retention
# LLM_PROVIDER, LLM_API_KEY, LLM_MODEL as in app.llm_factory
uv run uvicorn relayplane_app.app:app --port 8002
```

Register `https://<your host>/webhooks/relayplane/<RELAYPLANE_SUBSCRIPTION_ID>` as the subscription's
URL at the gateway. `INSECURE_LOCAL=1` allows plain HTTP and private addresses for the gateway and the
tool API, for a local sandbox only.

Run several replicas against the same database if you need to: conversations are leased, so no two
workers answer the same conversation and none sends a reply twice.

## Before production

- Run the live contract test against your deployed gateway and keep the evidence
  (`docs/RELEASE_EVIDENCE.md`).
- Scrape `/metrics` and load `ops/alerts.rules.yml`; on RelayPlane, `DeliveryUnknown` means the whole
  instance may be waiting for someone to resolve one message (`docs/OPERATIONS.md#unknown-delivery`).

Tested end to end against PostgreSQL and the gateway simulator: `tests/postgres/test_relayplane_app.py`.
