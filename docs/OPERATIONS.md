# Operations runbook

What to do when something fires, how to change things safely, and how to recover. Every alert in
[`ops/slo.yaml`](../ops/slo.yaml) points at a section here (a test keeps that true). The runtime keeps
ALL its work in PostgreSQL: every "queue" below is a table, so the first move is almost always to look
at the rows, not at the processes.

## Alerts

### inbox-latency
Inbound messages wait too long for a turn. Check `conversation_agent_turns_processing` and the number of
worker processes first: too few workers (scale out), or workers stuck (see *stuck-turn*), or a slow LLM
or tool (see *tool-failures*, *circuit-open*). The webhook already pushes back when a tenant's backlog is
over its threshold (503 + `Retry-After`); nothing is lost, the gateway redelivers.

### stuck-turn
A turn has been `PROCESSING` for minutes. Its worker lost the conversation lease (a lease expires on its
own and another worker takes the turn over, replaying the journal, so most of the time this clears
itself). If it does not: look at `turns` for the row, at `turn_journal` for the last step, and at the
worker logs for that `turn_id`. Never edit `turns` or `turn_journal` by hand: a divergent replay
fails closed (`JournalDivergenceError`) on purpose.

### outbox-delay
Replies are due and not sent. The outbox worker is down or the gateway is refusing (HTTP 429/5xx):
check the sender logs and the gateway status. Unsent rows are durable and are sent later with the same
idempotency key.

### unsettled-delivery
A reply was sent to the gateway but its acceptance was never confirmed, for longer than an hour. The
gateway only remembers the idempotency key for its retention window (`GET /api/v1/limits`,
`idempotency_retention_seconds`): past it, resending would DUPLICATE the message. The reconciler never
resends past `retention - margin` (the row becomes `UNPROVEN_PAST_RETENTION`). Decide by hand: look the
message up at the gateway (`GET /api/v1/messages/{id}` with the row's `channel_message_id`), and if the
gateway itself reports `UNKNOWN`, resolve it (`RelayPlaneSender.resolve`, audited). Do this BEFORE the
window closes.

### unknown-write
An external write's outcome is unknown and has not been resolved. The reconciliation worker retries
(`retry_same_key`) or looks the operation up (`status_lookup`) according to the tool's recovery contract;
`human_handoff` rows wait for a person. Check `tool_invocations` for the row, then the external system:
if the effect exists, mark it reconciled through the reconciler; if it does not and the contract allows
a same-key re-send, let the reconciler do it. NEVER re-send a write with a new key.

### handoff-backlog
Conversations are waiting for a person (`ownership = HANDOFF_PENDING`). Staff the queue, or return them
to the bot with `OwnershipService.return_to_bot` (audited).

### overdue-timers
Timers are past due: the scheduler worker is down or crashing. Timers are durable (`scheduled_events`);
they fire when a worker is back. Proactive messages that fire late still pass the channel policy.

### circuit-open
A breaker opened for a tenant+connection after repeated provider failures. Calls fail fast with
`CIRCUIT_OPEN` (nothing is sent) and it probes again after its window. Check the external system.

### tool-failures
Outbound tool calls fail for a provider. Distinguish by `error_code` in
`conversation_agent_tool_calls_total`: `EXTERNAL_5XX`/`EXTERNAL_TIMEOUT` (their side), `RATE_LIMITED` (our
per-tenant cap), `MCP_TOOL_SCHEMA_CHANGED` (a server changed a tool: review and re-pin, see *releases*).

### llm-errors
LLM calls are failing. Split by `error_code` and `provider` in `conversation_agent_llm_calls_total`. A
provider outage shows as a burst of `LLMProviderError`: turns stay open and are retried from the journal
(never lost); past `max_turn_attempts` a turn fails permanently with `turn.failed_permanently` in the
logs. If it is one model, the provider may have retired or renamed it (check the configured model).

### llm-latency
The p95 of LLM calls is high. Compare by `model` and `purpose`: a slow `agent` purpose points at long
prompts or the provider; slow `flow_understanding` or `confirmation_decision` calls point at a model
too heavy for a classification. Turn latency (inbound to reply) grows by the sum of its calls.

### llm-cost
Spend is above the ceiling you set in `ops/slo.yaml`. Find where: `usage_report` grouped by
`agent_version` (did a release get more expensive?), by `purpose` (confirmation or extraction calls
exploding?) and by `model`. Cached-input tokens are cheaper: a sudden drop of `cache_read` tokens means a
prompt change defeated the cache. A canary that costs more per conversation than stable is a reason to
roll back (`ReleaseManager.rollback`).

### llm-unpriced
Calls go to a model with no entry in `ops/llm_prices.yaml`: reported costs are a lower bound. Add the
model's price (per million tokens) from the provider's price page; history is re-priced on the next
report, nothing is rewritten.

### llm-usage-records
The usage ledger (`llm_usage`) rejected writes: calls happened and were not recorded, so cost reports
are incomplete for that window. Check the database; the answers to customers were NOT affected (a failed
record never fails a call). Estimate the gap from the provider's own invoice for the same window.

## Releases

Publishing a version makes it available; a **release** decides who gets it (`ReleaseManager`).

1. Run the eval suite against the exact agent: `python -m conversation_agent.app.evals agent.yaml
   suite.yaml [--cassette ...] --json report.json`. Exit code 1 blocks the release.
2. `promote` (everyone new) or `start_canary` (a percentage; the same conversation always lands on the
   same side and widening only adds conversations). Both need the report's evidence for that agent
   digest. Conversations with work in progress are never migrated.
3. Watch the SLOs. To widen: `set_canary_percent`. To pull: `rollback`: a canary is withdrawn; a bad stable
   version is withdrawn and the previous stable is restored. Idle conversations on a withdrawn version
   move off it on their next turn; conversations with a flow or a pending confirmation finish where they
   started. A withdrawn version can never be released again (publish a new one).

## Schema migrations

`python -m conversation_agent.app.migrate status|check|apply [--dry-run]` with `DATABASE_URL`.
Forward-only and checksummed: an applied migration file that changed stops everything
(`MigrationDriftError`); a database that is ahead of the code refuses to run old code
(`SchemaAheadError`). Deploy order for a schema change: expand (add the new shape, compatible with the
running code) -> apply -> deploy the code that uses it -> only in a later release, contract (drop the
old shape). `check` is the pre-deploy gate and `/readyz` reports the same.

## Personal data (LGPD)

`RetentionService`: `locate_contact` (what do we hold), `erase_contact` (the right to erasure),
`apply_retention` (the tenant's windows; `dry_run=True` first). Erasure refuses while a turn, a lease,
an unfinished external effect, an undelivered message or a pending confirmation is in motion, and says
which; `force` cancels only undelivered messages and pending confirmations. What other guarantees need
stays as a content-free tombstone (dedupe, idempotency, ledger). The audit trail records who and what
counts, never the person or the content.

## Backup and disaster recovery

PostgreSQL is the only durable state. Targets: **RPO** = what the backup strategy gives (point-in-time
recovery with WAL archiving gives seconds; nightly dumps give up to a day); **RTO** = time to restore plus
the reconciliation described below. Decide them per environment; the runtime tolerates any of them
because every side effect is idempotent or reconciled.

Restore procedure:

1. Restore the database (base backup + WAL to the chosen point, or a dump).
2. `python -m conversation_agent.app.migrate check` (the schema must match the code you start).
3. Run the integrity check (`verify_integrity`): it must report no violation; any violation names the rows.
4. Start the workers. They resume from the durable state by themselves: open turns replay from their
   journal, undelivered replies are sent, unknown writes are reconciled.
5. Know what a restore cannot know: anything that happened AFTER the restore point (messages received,
   replies sent, writes executed) is invisible to the database. Inbound messages come back through the
   gateway's redelivery; replies sent after the point may be sent again (same key, inside the gateway's
   window they are replays; past it they duplicate: compare the restore point with
   `idempotency_retention_seconds`); external writes that happened after the point are NOT in the ledger,
   so the system will not know they exist: reconcile those against the external systems by hand. Treat
   a restore older than the gateway's idempotency window as an incident, not a procedure.
6. Rehearse this. A backup that was never restored is a hypothesis.

## Audit

`admin_audit` is append-only (a database trigger refuses UPDATE and DELETE). It records agent
publishing, release decisions, ownership changes, retention policy and runs, and erasures, with the actor.
Read it with `PostgresAuditLog.list(tenant_id, action=..., subject_id=...)`.
