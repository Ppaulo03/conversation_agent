# Changelog

What changed for an application that embeds the framework. Internal detail lives in
`docs/05_REVIEW_LOG.md` and `docs/03_DECISIONS.md`; each entry names its finding or ADR.

## Unreleased

### Added

- **Tool selection (ADR-028)**: `tool_selection` in the agent manifest shrinks the tool list the model sees once
  `max_tools_per_call` and/or `max_tool_tokens` is exceeded. Off by default; `rules` provider; never adds a tool;
  decision journaled (`TOOL_SELECTION`); fallback to the full list; metric `tool_selections_total`.
  `TurnEngine` takes `tool_selector` (use `tool_selector_for(agent)`) when the agent enables it.

## 0.4.0

### Added

- **A list argument is shown in full in the confirmation question and in `executed_template`**: one `- item` per
  line, an object as `key: value, ...` (empty fields left out), so a write that takes a list (a weekly plan) can
  be confirmed by a `summary_template` with a single `{slots}` placeholder.

## 0.3.0

### Added

- **Wyrm channel: confirming protected actions without a quoted reply** (needs wyrm-sdk 0.9.0 and a provider
  that publishes timestamps and receipts). `dispatch_to_event` maps `payload.provider_timestamp` to
  `provider_occurred_at`; `WyrmSender` sends a confirmation prompt (`action_id` set) with
  `send_message_and_wait_receipt` and fills `provider_message_id` and `provider_accepted_at` from the receipt
  (`WyrmSender(publisher, receipt_wait_seconds=10.0)`). No receipt in time stays `ACCEPTED` without a provider
  clock, so the runtime asks again; a `REJECTED` receipt is `FAILED`. The `WyrmPublish` protocol gains
  `send_message_and_wait_receipt`: a publisher passed to `WyrmSender` must have it (the real `WyrmPublisher` does
  from wyrm-sdk 0.9.0). See `docs/WYRM_CHANNEL.md`.

## 0.2.0

### Added

- **Wyrm channel adapter** (`conversation_agent.adapters.wyrm`): dispatch -> `InboundEvent` mapper and
  `WyrmSender` with a zero-retry `DeliveryPolicy`; no `wyrm-sdk` dependency. Text-only, private chats
  only, no provider evidence (protected actions cannot be confirmed). See `docs/WYRM_CHANNEL.md`.

### Breaking: what an embedding application must change

- **`Runtime.build` requires `delivery_policy`** (ADR-023, F-010). There is no default resend window
  any more, because only the channel knows how long it remembers an idempotency key. A call without
  it now fails with `TypeError: missing 1 required keyword-only argument: 'delivery_policy'`.
  - RelayPlane: `policy = await sender.delivery_policy(tenant_id, retry_horizon=timedelta(hours=1))`
    reads the retention the deployed gateway reports (`GET /api/v1/limits`) and refuses a horizon
    that does not fit. See `examples/relayplane-app`.
  - Any other channel: `DeliveryPolicy(idempotency_retention=..., retry_horizon=...)`, with the
    retention your channel guarantees; `retry_horizon=timedelta(0)` means never resend.
  - Tests: a fixed policy such as `DeliveryPolicy(idempotency_retention=timedelta(hours=24),
    retry_horizon=timedelta(hours=1))`.
- Renamed with no alias (ADR-023): `outbox_retry_horizon` is gone (use the policy);
  `DeliveryPolicy.reconcile_margin` is `safety_margin`; `safe_resend_until` is `safe_resend_window`.
  Only code that builds `OutboxWorker`/`OutboxStore` by hand also sees `policy`/`coordination` on the
  worker, keyword `unknown_blocks_for` on `claim_ready` and `with_handle` on `claim_unsettled`.
- A custom `MessageSender` may no longer return `SendResult(status=QUEUED)` without a
  `channel_message_id` (the handle the reconciler looks the message up by).
- **The schema is no longer migrated or stamped by a check** (ADR-026, F-014). Run
  `python -m conversation_agent.app.migrate apply` as a release step, and call
  `await db.ensure_schema_current()` before starting the runtime (the example does). A never-migrated
  database now fails with `SchemaOutOfDateError` instead of being touched.
- `InboundEvent` with an empty `event_id` fails at construction (ADR-025, F-012).
- `MigrationStatus.state` can be `gap`, and `apply`/`ensure_current` fail closed with
  `MigrationHistoryError` on a non-contiguous history (ADR-027, F-015).

### Added

- `examples/relayplane-app`: a complete ASGI application on RelayPlane (webhook, runtime, operational
  endpoints), tested end to end against PostgreSQL and the gateway simulator.
- Webhook schema version 2 from RelayPlane is accepted alongside version 1.
- Metrics `conversation_agent_outbox_unknown` and `..._outbox_oldest_unknown_age_seconds`, alert
  `DeliveryUnknown` and runbook *unknown-delivery*: on RelayPlane one `UNKNOWN` message holds every
  later send of its instance until someone resolves it.
- CI (`.github/workflows/ci.yml`) running the official gates, the PostgreSQL suite and
  `ops/validate_artifacts.py` (alerts and dashboard checked by `promtool` and a real Grafana).

### Fixed

- A duplicate inbound redelivery no longer creates conversation state (F-013).
- `RelayPlaneSender.delivery_policy` says why it cannot reach the gateway (for example, a plain
  `http://` URL on a connection that requires TLS) instead of a generic "connection/credential
  unavailable".
