# Release evidence: validating the RelayPlane contract

The framework's delivery guarantees (INV-021, INV-007) rest on what the deployed gateway really does.
Before a production pilot, run the live contract test against it and keep the result with the release.

## 1. Run

```bash
export RELAYPLANE_BASE_URL=https://<your gateway>
export RELAYPLANE_TOKEN=<API token>
export RELAYPLANE_CHANNEL_ID=<instance id>
export RELAYPLANE_TO=<a test number you control>
export RELAYPLANE_RETRY_HORIZON_SECONDS=600      # the sender's retry horizon (must be <= retention)
uv run pytest tests/contracts/test_relayplane_live.py -m integration -v
```

It sends real messages to `RELAYPLANE_TO`. It checks that the same `Idempotency-Key` twice is ONE
message, and that the retention the gateway reports (`GET /limits`) is at least the retry horizon.

## 2. Also check by hand (webhook side)

- the signature of a real delivery verifies (`X-RelayPlane-*` headers, your `whsec_...` secret);
- a real `message.received` has the fields the adapter reads (`instance_id`, `from`,
  `provider_message_id`, `reply_to_provider_message_id`, `timestamp`);
- `message.status` events arrive for a send (`QUEUED` then `ACCEPTED`), carrying `provider_accepted_at`.

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

Keep this table with the release notes. A change of gateway version or of its retention means doing
it again.
