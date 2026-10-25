# Changelog

What changed for an application that embeds the framework. Internal detail lives in
`docs/05_REVIEW_LOG.md` and `docs/03_DECISIONS.md`; each entry names its finding or ADR.

## Unreleased

### Added

- **`confirmation_review` on a capability** names a read the runtime runs on the proposed arguments when a protected
  capability is proposed; the strings in its `confirmation_review_field` output (default `warnings`) are listed
  above the confirmation question. The person always sees them, without depending on the model calling the check
  and telling them first. A review that fails adds nothing and never blocks the proposal. It must be a read
  without confirmation, with inputs and output field that exist (`REVIEW_UNKNOWN_CAPABILITY`,
  `REVIEW_WITHOUT_CONFIRMATION`, `REVIEW_NOT_A_READ`, `REVIEW_UNKNOWN_INPUT`, `REVIEW_UNKNOWN_FIELD`). Agents that
  do not set it are unchanged, and so are their manifest digests.

## 0.8.0 - 2026-10-25

### Added

- **`separate_turn_from` on a capability** names capabilities that must not have run earlier in the same turn. A
  call in that turn is denied (`SAME_TURN_AS:<CAPABILITY>`) with a message telling the model to answer the person
  first and call it in a later turn. Use it when the person has to hear what a check found (for example a plan
  review) before the action runs; a prompt alone is not reliable for that. Unknown names do not compile
  (`SEPARATE_TURN_UNKNOWN_CAPABILITY`). Agents that do not set it are unchanged, and so are their manifest digests.

## 0.7.0 - 2026-10-24

### Added

- **`max_output_tokens` on the agent** caps the tokens one model call may produce (the default stays 1024). A
  reasoning model (for example `gpt-oss`) spends hidden thinking tokens inside that cap, so a long answer could end
  with a `max_tokens` stop and no text, and the person got the fallback reply. Agents that do not set it are unchanged,
  and so are their manifest digests.

## 0.6.1 - 2026-10-24

### Fixed

- **A 0.5.0 database could not be upgraded to 0.6.0.** The contact-memory migration was numbered `0026_contact_profiles`,
  but `0026_turn_deferred_until` and `0027_confirmation_budget_deferred` already existed: on a database at 0.5.0 it
  sorted before two applied migrations, so the migrator refused it as a gap in the history
  (`MigrationHistoryError`; `ensure_current` failed the same way). It is now `0028_contact_profiles` and a test fails if
  two migrations ever share a number.
  A database created from scratch with 0.6.0 recorded the old name; rename it once
  (`UPDATE schema_migrations SET version = '0028_contact_profiles' WHERE version = '0026_contact_profiles'`, the
  file content and its checksum are unchanged) or recreate it. Upgrading from 0.5.0 needs nothing.

## 0.6.0 - 2026-10-24

### Added

- **Contact memory store (ADR-032, INV-081)**: a `ContactMemory` port, `InMemoryContactMemory`,
  `PostgresContactMemory` and migration `0028_contact_profiles` (shipped as `0026_` in 0.6.0, see 0.6.1) keep a display name and short facts per
  `(tenant_id, contact_id)`, independent of any conversation. New optional `memory` block in the manifest and
  `AgentDefinition.memory` (`max_facts`, `max_fact_chars`, `max_name_chars`); without it the digest does not change.
  With the block, the turn loads the contact's profile (journaled), shows it in the system prompt as quoted data
  and offers `memory__set_name`, `memory__remember` and `memory__forget`; each write is a journaled step (INV-082).
  `Runtime.build`, `build_memory_engine` and `TurnEngine` take `contact_memory=` (PostgreSQL by default in
  `Runtime.build`); a `memory.` capability prefix is reserved for those agents. Agents without the block are
  unaffected.
  `RetentionService.erase_contact` also removes the profile and takes `keep_profile=True` to erase only the
  conversations (a reset); `RetentionPolicy.memory_days` (default `None`, never) expires profiles (INV-083).

## 0.5.0 - 2026-10-24

### Added

- **Tool selection `jev` provider (ADR-031)**: ranks tools with TypeSafe Jev through an operator connection
  (key from the SecretProvider, never in the manifest): one `choice` question over the candidate tools, probabilities
  rescaled to scores, same guards, circuit breaker and full-list fallback as `http`. `--provider jev --token-env VAR`
  in the evaluation runner. The wire format is from public documentation and not yet verified against a live key.

- **Circuit breaker for the `http` tool selector (ADR-030)**: after `failure_threshold` consecutive health failures
  the server is not called for `open_seconds` and the model gets the full list (outcome `circuit_open`);
  `tool_selection.http.circuit` (on by default), transitions in `circuit_transitions_total{provider="tool_selector"}`.
  The state machine moved to `adapters/circuit.py` and is shared with the tool breaker (no behavior change).
  `Runtime.build`/`build_memory_engine` take `tool_metrics=` for the transitions.

- **Tool selection `http` provider (ADR-029)**: an optional external ranking server, reached through a
  pre-registered tenant connection with the same outbound guards as tools; versioned contract in
  `docs/TOOL_SELECTOR_CONTRACT.md`, reference server in `examples/reference-tool-selector/`, and
  `--provider http --url ...` in the evaluation runner. `ToolSelector.rank` now takes `tenant_id`;
  pass `tool_selection_guard=` to `Runtime.build`/`build_memory_engine` (or `guard=` to
  `tool_selector_for`) when an agent uses it. Any failure sends the model the full list.

- **Tool selection evaluation**: `python -m conversation_agent.app.tool_selection_eval` measures any `ToolSelector`
  (`rules` or `package.module:factory`) over a labelled JSONL dataset: hit rate, per-tool recall, fallback rate and
  tools/tokens removed. `examples/tool-selection-eval/` ships an 8-tool catalog, 51 + 16 Portuguese messages and rules.
- The `rules` score is now `1 - 0.5**matched keywords` (it used matched/declared, which sank tools with long keyword
  lists below `min_score`; found by the evaluation).

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
