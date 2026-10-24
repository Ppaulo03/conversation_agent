# Operations runbook

What to do when something fires, how to change things safely, and how to recover. Every alert in
[`ops/slo.yaml`](../ops/slo.yaml) points at a section here (a test keeps that true). The runtime keeps
ALL its work in PostgreSQL: every "queue" below is a table, so the first move is almost always to look
at the rows, not at the processes.

`conversation_agent` is an embeddable framework with no canonical deployment runtime (ADR-022). Where this
runbook touches infrastructure it separates the **Framework contract** (what the library guarantees and what
the consuming application must respect for those guarantees to hold) from the **Operator responsibility**
(what the consuming application chooses and runs: process model, ASGI server, PostgreSQL provisioning,
backups, observability backends, CI/CD). The alert sections below describe framework behaviour; the
action they ask for is the operator's.

## Where to look

| Question | Where |
|---|---|
| What happened to this conversation? | JSON logs filtered by `conversation_ref` (compute it with `core.observability.conversation_ref(tenant, conversation_id)`) |
| Where did this message go? | logs/spans filtered by `trace_id`; it is also in `inbox_events.trace_id`, `outbox_messages.trace_id` and `tool_invocations.context_json` |
| Is the system healthy? | `/metrics` (queue backlogs and ages, turn outcomes and latency), the SLO alerts |
| What does the LLM cost, and where? | `python -m conversation_agent.app.usage --tenant T --since D --until D --by agent_version,purpose` |
| Did this release get dearer? | `python -m conversation_agent.app.usage ... --agent A --compare STABLE,CANDIDATE` (exit 1 = worse) |
| Is a tenant over budget? | `conversation_agent_llm_budget_used_ratio`, `BudgetEvaluator.status(tenant)` |
| What did an operator change? | `admin_audit` (`PostgresAuditLog.list`) |

Wiring: `setup_observability()` turns on JSON logs (`LOG_LEVEL`, `LOG_STACK`), spans (`TRACE=log|otlp|off`)
and loads the price list (`LLM_PRICES_FILE`); wrap the LLM with `wrap_llm` (it goes on the books) and mount
`ops_app` on a non-public port.

`TRACE=otlp` needs `conversation-agent[otel]` and uses the standard OpenTelemetry environment variables
(`OTEL_SERVICE_NAME`, `OTEL_EXPORTER_OTLP_ENDPOINT` or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, headers,
certificate and timeout). It exports OTLP/HTTP in batches; point it at an OpenTelemetry Collector in
production. Call `Observability.shutdown()` from the deployment's shutdown hook so the final batch is
flushed. The durable framework `trace_id` is exported as `conversation_agent.trace_id` for correlation
with rows and logs. W3C `traceparent` extraction/injection remains a channel/HTTP-adapter concern: the
host does not invent propagation for protocols that do not define it.

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

### unknown-delivery
A reply is `UNKNOWN`: the channel could not say whether it went out. A transport `UNKNOWN` (timeout,
5xx) is retried by the reconciler with the same idempotency key and usually clears on its own; one
that stays past a few minutes is almost always an `UNKNOWN` the GATEWAY recorded (ambiguous dispatch
to the provider), and that one needs a person.
On RelayPlane this is urgent: an `UNKNOWN` message is an ORDER BARRIER for its instance. Every later
send of that instance (every conversation on that number) waits behind it until someone calls
`POST /api/v1/messages/{id}/resolve`, or until the gateway's `UNKNOWN_BARRIER_TIMEOUT` releases it if
the operator configured one (`docs/FAILURE-MODES.md` in RelayPlane). Expect `OutboxDelay`/`DeliveryUnsettled`
to follow for the other conversations.
What to do: find the row (`outbox_messages WHERE status = 'UNKNOWN'`, its `channel_message_id`), ask the
gateway (`GET /api/v1/messages/{id}`) and, if the provider shows whether it was delivered, resolve it
with `RelayPlaneSender.resolve` (audited). When nobody can tell, resolving as `not_sent` releases the
queue; the contact did not get that one reply, so follow up by hand if it mattered. The automation
never resolves (`CHANNEL_UNKNOWN_NEEDS_DECISION`).

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

### turn-latency
Turns take long to process. `conversation_agent_turn_processing_seconds` is the time from taking the
conversation lease to committing the reply; compare by `agent_version` (a release that got slower), then by
what the turn did: many `turn_llm_calls_total` per turn means a long agent loop, slow tools show in
`tool_call_duration_seconds`, slow models in `llm_call_duration_seconds` by `purpose`.

### turn-failures
Turns fail permanently (`turn.failed_permanently` in the logs, filter by `agent_version`): almost always the
LLM provider failing past `max_turn_attempts` (see *llm-errors*) or a journal divergence
(`turn.journal_divergence`: a deploy changed what a turn does while it was open; the turn fails closed on
purpose). A rate that started with a release is a reason to roll it back.

### handoff-rate
Many turns end by handing the conversation to a person. Compare by `agent_version`: a release that makes the
agent give up more is a regression the eval suite did not cover; if it is a tool failing, the flows' fallback
is a handoff, so look at *tool-failures* first.

### llm-budget
A tenant is near or over its LLM budget (`LLMBudget`: tokens and/or USD, per day and per month; set with
`PostgresBudgetStore.set`, audited). What happens when it is exceeded is `on_exceed`:

- **`degrade` (the default):** nothing is lost. Messages are accepted and stored; a turn that would START new
  work does not call the model: the contact gets ONE fixed notice per exceeded period (the agent's
  `budget_exceeded_reply`, in its language; a default English text otherwise) and later messages in that period are
  kept but not answered. A flow in progress, a pending confirmation or a resumed turn finishes normally. Needs the
  runtime to be given the gate: `Runtime.build(budget=BudgetEvaluator(...))`. When the period resets the agent
  answers again; messages that arrived meanwhile were not answered, which the notice told the contact.
- **`alert`:** nothing is refused or changed: the status and the metric say so. Decide whether to raise the budget
  or find the cause (`app.usage --tenant T --by agent_version,purpose`: a loop, a release that got dearer, a flood
  from one contact).
- **`refuse_new`:** the edge refuses NEW user messages with 503 and a `Retry-After` until the period resets;
  messages in flight finish. A gateway gives up redelivering after its own retry budget, so a long refusal can lose
  messages: it protects the bill, not the customer.
- **`defer_llm`:** nothing is lost and nothing is answered late with a stale context: messages are accepted and
  stored; the runtime pauses an open turn immediately before an LLM request, stores the reset time in the turn and
  resumes it after the period rolls over; deterministic paths (Flows) continue. A pending protected action whose
  answer needed LLM interpretation is re-prompted at once, so a former "yes" can never execute after the reset.
  Wire the evaluator with `Runtime.build(..., llm_gate=evaluator)`. Choose it over `degrade` when the contact
  should get a real answer once the budget is back.

USD figures are a lower bound while models are unpriced (*llm-unpriced*). The budget is evaluated from the usage
ledger and cached for seconds: a tenant can overshoot by what it spends inside one cache window. A budget saved
before `degrade` existed keeps the `on_exceed` it was saved with.

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
Forward-only and checksummed: an applied migration file that changed makes `apply` stop
(`MigrationDriftError`); a database that is ahead of the code is refused by `apply` and by the
verification (`SchemaAheadError`); recorded migrations must be a contiguous prefix of the known ones, so a
missing migration with a later one recorded is refused by `apply` (also `--dry-run`) and by the verification
(`MigrationHistoryError`, ADR-027). Deploy order for a schema change: expand (add the new shape) -> apply ->
deploy the code that uses it -> only in a later release, contract (drop the old shape). `check` is the
pre-deploy gate and `/readyz` reports the same.

**Migrating and verifying are different operations (ADR-026).** `apply` (`PostgresDatabase.migrate()`) changes the
schema and is always an explicit step. Verification only reads: `status`, `check`, `/readyz` and
`await db.ensure_schema_current()` create nothing (not even `schema_migrations` on a fresh database), stamp
nothing and take no lock, so they are safe on every start and need no DDL rights. The framework does NOT verify
by itself: `Runtime.build/receive/run/tick` and the workers never check the schema. Your application calls the
preflight before any durable work:

```python
db = await PostgresDatabase.connect(DATABASE_URL)
await db.migrate()                # only when this process is the one that migrates (explicit)
await db.ensure_schema_current()  # every start: SchemaOutOfDateError / SchemaAheadError / MigrationHistoryError / MigrationDriftError
runtime = Runtime.build(db=db, ...)
```

Errors: `SchemaOutOfDateError` (behind, or never migrated), `SchemaAheadError` (the database holds migrations this
build lacks; downgrading the code is not supported, restore or run the newer code), `MigrationDriftError` (an
applied migration file changed; there is no override), `MigrationHistoryError` (a known migration is missing
while a later one is recorded: `apply` is NOT the remedy and never runs the missing one; investigate how the metadata
was lost, restore or reconcile it by hand; `status` shows the migration as `gap`). Precedence: drift, ahead, gap,
out of date. All four are in `conversation_agent.adapters.postgres.migrator`;
connection failures propagate as the driver raised them. `verify_integrity` includes this check and then looks at the
data; it is not a startup requirement.

**Mixed versions:** the check is exact. After newer migrations are applied, older code sees the database as ahead and
an old instance that re-verifies (`/readyz`) becomes unready. There is no verified mixed-version rollout contract:
complete the migration before any process of the newer version starts durable work.

**PostgreSQL version:** developed and tested against PostgreSQL 16 (`postgres:16-alpine`). Other versions have not been
verified; there is no formal multi-version support matrix.

**Framework contract:** migrations are forward-only and checksummed; `apply` refuses a database that is ahead of the code or
whose applied files changed, and is serialized by an advisory lock; verification is read-only.
**Operator responsibility:** when and how to run `apply` in your release process, calling the schema preflight before durable
work starts, and the rollout order around it.

## Personal data (LGPD)

`RetentionService`: `locate_contact` (what do we hold), `erase_contact` (the right to erasure),
`apply_retention` (the tenant's windows; `dry_run=True` first). Erasure refuses while a turn, a lease,
an unfinished external effect, an undelivered message or a pending confirmation is in motion, and says
which; `force` cancels only undelivered messages and pending confirmations. What other guarantees need
stays as a content-free tombstone (dedupe, idempotency, ledger). The audit trail records who and what
counts, never the person or the content. The name and facts an agent remembers (contact memory) are erased with the
contact; `keep_profile=True` erases only the conversations (a reset), and `RetentionPolicy.memory_days` expires
profiles that have not changed for that long (off by default).

## Backup and disaster recovery

PostgreSQL is the only durable state of the framework's runtime. The framework does not own backups, storage,
snapshot policy, RPO/RTO or restore rehearsals (ADR-022); it owns the conditions that must hold after a restore
for its guarantees to remain valid.

### Framework contract

After any restore, before the workers start:

1. **Schema compatible with the code you start:** `python -m conversation_agent.app.migrate check` (or
   `await db.ensure_schema_current()`) must pass (`SchemaOutOfDateError` / `SchemaAheadError` / `MigrationHistoryError` / `MigrationDriftError` otherwise).
2. **Integrity:** `python -m conversation_agent.app.integrity` (`verify_integrity`) must report no violation; any
   violation names the rows.
3. **Workers resume from the durable state by themselves:** open turns replay from their journal, undelivered
   replies are sent, unknown writes are reconciled. Start them only after 1 and 2.
4. **What a restore cannot know:** anything that happened AFTER the restore point (messages received, replies
   sent, writes executed) is invisible to the database. Inbound messages come back through the gateway's
   redelivery; replies sent after the point may be sent again (same key: inside the gateway's window they are
   replays, past it they duplicate); external writes that happened after the point are NOT in the ledger, so the
   system will not know they exist and they must be reconciled against the external systems by hand.
5. **Idempotency window:** a restore point older than the channel's idempotency window
   (`idempotency_retention_seconds`) breaks the delivery guarantee (INV-034/061/066). Treat it as an incident, not
   a procedure. The runtime's `DeliveryPolicy` (`retry_horizon <= idempotency_retention - safety_margin`) must stay within that window.
6. **Shutdown:** the consuming application's lifecycle must let the workers stop cooperatively (`run(stop)`; a
   turn stops at a safe boundary, INV-012) so a stop or redeploy does not need a restore.

### Operator responsibility

Backup tool and frequency, backup retention, storage, encryption, restore rehearsal ("a backup that was never
restored is a hypothesis"), and the RPO and RTO you commit to. Deployment topology, process management and
provisioning are also yours. The framework prescribes no tool, platform or backup mechanism.

What those choices mean for the framework's guarantees:

- **RPO** is yours to choose, and it can affect the guarantees: everything after the restore point is missing from
  the restored ledger and outbox (see items 4 and 5 above). A restore point older than the channel's relevant
  idempotency window can invalidate delivery guarantees.
- **RTO** is yours to choose; by itself it does not change the framework's consistency model (it only decides how
  long the system is down; the durable state resumes as in item 3).
- **External effects after the restore point** may not exist in the restored ledger. Treat them as an incident:
  reconcile them against the external systems by hand.
- Choose a backup strategy compatible with the guarantees your application requires. The framework does not make
  an arbitrary RPO safe.

## Audit

`admin_audit` is append-only (a database trigger refuses UPDATE and DELETE). It records agent
publishing, release decisions, ownership changes, retention policy and runs, and erasures, with the actor.
Read it with `PostgresAuditLog.list(tenant_id, action=..., subject_id=...)`.
