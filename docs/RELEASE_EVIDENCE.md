# Optional adapter evidence: validating the RelayPlane contract

RelayPlane is an optional adapter behind the framework's channel ports. The framework does not require
RelayPlane, and publishing or deploying it with another channel does not depend on this evidence.

When a deployment selects the RelayPlane adapter, its delivery guarantees (INV-021, INV-007) also rest
on what that deployed gateway really does. Before a production pilot with RelayPlane, run the live
contract test against it and keep the result with that deployment's release evidence.

## 1. Run

```bash
export RELAYPLANE_BASE_URL=https://<your gateway>
export RELAYPLANE_TOKEN=<API token>
export RELAYPLANE_CHANNEL_ID=<instance id>
export RELAYPLANE_TO=<a test number you control>
export RELAYPLANE_RETRY_HORIZON_SECONDS=600      # the sender's retry horizon (must be <= retention)
# export RELAYPLANE_ALLOW_INSECURE=1             # ONLY for a local sandbox on plain HTTP / a private address
uv run pytest tests/contracts/test_relayplane_live.py -m integration -v
```

It sends real messages to `RELAYPLANE_TO`. It checks that the same `Idempotency-Key` twice is ONE
message, and that the retention the gateway reports (`GET /limits`) is at least the retry horizon.

## 2. Also check by hand (webhook side)

- the signature of a real delivery verifies (`X-RelayPlane-*` headers, your `whsec_...` secret);
- a real `message.received` has the fields the adapter reads (`instance_id`, `from`,
  `provider_message_id`, `reply_to_provider_message_id`, `timestamp`);
- `message.outbound_status` events arrive for a send (`QUEUED`, then `ACCEPTED`), carrying `accepted_at`
  (`message.status` is a different event, the provider's receipts such as delivered/read, and the
  adapter ignores it).

## 3. Record

| Field | Value |
|---|---|
| Date | |
| conversation_agent commit | `git rev-parse HEAD` |
| RelayPlane version / commit / deployment | |
| Gateway idempotency retention (from `/limits`) | |
| Configured `DeliveryPolicy` (retention, retry horizon, margin) | |
| Live contract test result (passed / skipped / failed) | |
| Signature and event shape checked by hand | yes / no |
| Notes (anything that differed from `docs/RELAYPLANE_CONTRACT.md`) | |

### Recorded runs

| Field | 2026-10-17 (sandbox) |
|---|---|
| Date | 2026-10-17 |
| conversation_agent commit | copy of the project folder on 2026-10-17 (no git there; `version = 0.1.0`) |
| RelayPlane version / commit / deployment | project-folder copy, `VERSIONS.md` 0.2.0, local sandbox (`deploy/docker/sandbox.sh`) with the **provider simulator** |
| Gateway idempotency retention (from `/limits`) | 86400 s |
| Configured `DeliveryPolicy` (retention, retry horizon, margin) | 24 h, 600 s (test) / 1 h (application), 1 min |
| Live contract test result (passed / skipped / failed) | 2 passed, 1 skipped (the 24 h retention measurement is opt-in); run with the sandbox connection policy, now `RELAYPLANE_ALLOW_INSECURE=1` |
| Signature and event shape checked by hand | yes: signed `message.received` and `message.outbound_status` deliveries accepted and applied end to end (POC `relay_app`) |
| Notes (anything that differed from `docs/RELAYPLANE_CONTRACT.md`) | none in the contract. Simulated provider, so no real WhatsApp number; a provider 500 left one message `UNKNOWN` and held the whole instance until `resolve`, as RelayPlane documents (`OPERATIONS.md#unknown-delivery`). Source: `analysis/poc-integration-validation-2026-10-17.md` in the project |

A production pilot still needs a row like this one against the deployed gateway and a real number.

Keep this table with the release notes of deployments that use RelayPlane. A change of gateway version
or of its retention means doing it again.
