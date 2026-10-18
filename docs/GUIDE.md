# Usage guide

How to take an agent from a manifest to a running conversation, and what your channel must give
the runtime. (Design rationale lives in the Portuguese documents: `DESIGN.md`, `INVARIANTS.md`,
`RUNTIME_PROTOCOL.md`. This guide is for people *using* the framework and is written in English,
like the README and the code.)

## 1. What you are building

The framework owns **conversation state**, never business state. An *agent* is a manifest
(YAML) describing a persona, the capabilities it may use, the tools behind them, optional Flows
(scripted multi-step collection) and the wording of confirmations. A **protected action** (anything
that changes the outside world) is never executed from a model's say-so: it is *proposed*, the
contact *confirms* it, and only then does the runtime execute it, exactly once.

```
inbound event ──> inbox ──> turn (rules, flows, LLM, tools) ──> outbox ──> channel
                                   │ proposal             ▲
                                   └── pending action ────┘ "yes" ──> confirmed ──> executed
```

## 2. Install

```bash
pip install "conversation-agent[postgres]"    # the durable runtime (asyncpg)
pip install "conversation-agent[anthropic]"   # only if you use the Anthropic provider
```

The base install (pydantic, httpx, pyyaml) is enough to compile manifests, run evals and build an
in-memory engine. A missing extra is reported by name when you import what needs it.

## 3. The loop: compile, evaluate, run

```bash
# 1. does it compile? (prints agent, version, digest; every diagnostic otherwise)
python -m conversation_agent.app.compile agent.yaml [--packs DIR]

# 2. do its scenarios still pass? (offline: tools come from a recorded cassette)
python -m conversation_agent.app.evals agent.yaml evals.yaml --cassette evals.cassette.json

# 3. talk to it, durably, in the terminal
python -m conversation_agent.app.migrate apply
python -m conversation_agent.app.serve agent.yaml --http my_api=http://127.0.0.1:8001
```

`examples/support-agent/` is a complete, small agent with evals; `examples/vertical-slice/` is a
scheduling agent with a reference API. For `serve` set `DATABASE_URL` and the LLM variables
(`LLM_PROVIDER`, `LLM_API_KEY`, `LLM_MODEL`, see `.env.example`).

Without a database, `conversation_agent.app.wiring.build_memory_engine(...)` gives an in-memory
engine: reads and Flows work end to end, protected actions stop at the proposal.

## 4. Embedding the runtime in your service

`Runtime.build` assembles everything the reliability tests prove correct (turns under a lease,
the ledger executor, confirmation, the outbox, timers, reconciliation):

```python
from conversation_agent.adapters.postgres.db import PostgresDatabase
from conversation_agent.app.runtime import Runtime
from conversation_agent.app.wiring import load_agent, local_http_provider

db = await PostgresDatabase.connect(DATABASE_URL)
# Migrating is a separate, explicit step (`app.migrate apply`, or `await db.migrate()` in the process that migrates).
# Verifying is read-only and yours to call, before any durable work (Runtime never does it for you):
await db.ensure_schema_current()  # SchemaOutOfDateError / SchemaAheadError / MigrationDriftError
runtime = Runtime.build(
    db=db,
    compiled=load_agent("agent.yaml"),
    llm=llm,  # app.llm_factory.build_llm(...)
    providers={"http": my_http_provider},  # how the manifest's tools are served
    sender=my_channel_sender,  # delivers outbox rows (see §5)
)

await runtime.receive(inbound_event)  # from your webhook; duplicates are ignored
await runtime.run(stop_event)  # or: while True: await runtime.tick()
```

Bootstrap or upgrade: run the migration as its own explicit step first (`python -m conversation_agent.app.migrate apply`),
then the verification above. Only `apply` creates or changes the schema; `ensure_schema_current` leaves a database that was
never migrated untouched and raises `SchemaOutOfDateError`. The sequence is yours (ADR-022); this is not a production
entrypoint. Developed and tested against PostgreSQL 16 (`postgres:16-alpine`); other versions are unverified and there is no
formal multi-version support matrix. Mixed-version rollouts are not a verified contract (OPERATIONS, "Schema migrations").

**Several agents in one database.** Each runtime handles only the conversations of its *scope*
(default: its agent's id; override with `Runtime.build(scope=...)`). The scope is fixed when a
conversation is first seen: `Runtime.receive` stamps it, and a webhook takes it from the
subscription (`Subscription.scope`), never from the payload. A runtime never claims another
scope's turns or outbox messages, and a conversation cannot move between scopes. Conversations
pinned to an agent before scopes existed were assigned to that agent's scope by the migration; any
other legacy conversation (no scope) is adopted by the first scoped event that arrives for it and
is invisible to scoped runtimes until then. Several workers *of the same scope* share the work as
below.

`Runtime.build` requires a `delivery_policy` (there is no default and no "unbounded"): how long your
channel remembers an idempotency key (`idempotency_retention`), the `safety_margin` kept inside it, and
the `retry_horizon` for direct retries of the same delivery (a retryable failure, a recovered crash), with
`0 <= retry_horizon <= idempotency_retention - safety_margin`. The horizon is not the last moment a delivery can be
resent: an unknown send without a handle may still be resent by the reconciler after it, until
`idempotency_retention - safety_margin` (the safe resend window) closes; `0` disables every resend. `retry_horizon=timedelta(0)`
(`DeliveryPolicy.no_retry(...)`) means a delivery is never sent twice; use it for a channel whose
deduplication does not survive a restart (the console channel does). One policy covers every delivery
the Runtime handles: with channels of different guarantees use the conservative values or separate
runtimes. Every resend reuses the row's idempotency key and payload, and the channel must honour that
key for the period you configured.

If your channel can answer `queued` or `unknown` and settle it later, it needs a way to do so: implement
`lookup(message)` (`MessageLookup`; `Runtime.build` finds it on the sender, or pass `lookup=`) so the
Runtime asks the channel instead of sending again, and/or push the final status with
`OutboxStore.apply_channel_status`. Either is enough; a push-only channel is valid, and an unsettled
`queued` send then simply waits for the push (it is never re-sent). `queued` must carry the channel's
`channel_message_id`; a send that fails with proof is `FAILED`, an exception out of `send` is `unknown`.

Messages of one conversation always leave in the order they were written, even with several workers
or `split_replies`: each gets a sequence number from its conversation and a worker only claims a message
whose earlier ones are no longer waiting to go out. A message whose outcome is unknown holds the next
one back only while the channel still remembers its idempotency key (the policy's window).

Several workers can run against one database: leases and fencing keep one owner per
conversation, and coordination time is the database's clock. Give each its own `owner` (a random
one is generated by default). A worker that dies is
recovered after `lease_ttl` (`Runtime.build(lease_ttl=timedelta(...))`, default 30 s; it also bounds the
outbox, timer and reconciliation claims). A running turn renews its lease every
`heartbeat_interval_seconds` (default: a third of the TTL, up to 10 s), and the TTL must be more than
twice that, so a slow turn never loses its lease.

`local_http_provider` is for local development (plain HTTP, private networks). In production
resolve connections with a resolver so destinations are checked (`HTTPToolProvider` with a
`ConnectionResolver`); MCP servers are served by `MCPToolProvider` (`adapters/tools/mcp.py`).

**Messages sent in pieces.** A turn takes everything that is ready for the conversation, in delivery
order, joined by line breaks, and answers once. By default it opens as soon as a message is seen, so a
contact who types in bursts a few seconds apart can get an answer "in the middle". Two opt-ins on
`Runtime.build`:

| Option | Effect |
|---|---|
| `debounce=timedelta(seconds=3)` | the turn opens only after the contact has been quiet that long; each new piece restarts the wait, up to `debounce_max_wait` (default 10 s) since the first waiting piece. It delays EVERY answer by up to the debounce, so keep it short (2 to 4 s for WhatsApp). Measured on the database clock. |
| `restart_on_new_message=True` | a message arriving while a turn runs asks it to stop at its next safe point and reopen with everything together. Work already paid for (an LLM call) is discarded; if an irreversible effect already happened in that turn, it finishes and the new message gets the next turn. |

Both are off by default (nothing changes for existing deployments); they combine.

## 5. What your channel must provide

Confirmation is the one place where the channel's evidence matters. The runtime executes a
protected action only when it can **prove the "yes" answers the prompt it sent**.

**The sender** (`MessageSender.send(OutboundMessage) -> SendResult`) must:

- dedupe on `message.idempotency_key` (the same message may be sent again after a crash);
- return `status=ACCEPTED`, a `provider_message_id`, and **`provider_accepted_at` in the channel's
  own clock** when the provider exposes it. The runtime never invents this timestamp.

**Your inbound adapter** (a webhook handler, a poller, a bot SDK callback) is what builds
`InboundEvent`s. The runtime does not authenticate anything: it trusts the event it is handed. The
journey, and the contract for each field (ADR-025):

```text
authenticate the provider delivery        (HMAC, token, mTLS, SDK session: whatever the channel has)
  -> derive trusted identity              (tenant, channel, contact, conversation, scope)
  -> build InboundEvent                   (a pydantic error here = a bug in the adapter)
  -> await runtime.receive(event)
       True   a new logical event was persisted        -> acknowledge
       False  a duplicate delivery, nothing was added  -> acknowledge (it is a success)
       ConversationIdentityConflictError               -> permanent: do not retry (HTTP 409)
       any other exception                             -> durable acceptance NOT confirmed: do
                                                          NOT acknowledge (HTTP 5xx); redeliver the
                                                          SAME event_id: if the first attempt had
                                                          committed you get False, else it is stored
```

`ConversationIdentityConflictError` lives in `conversation_agent.core.errors`.

| Field | Contract |
|---|---|
| `tenant_id`, `channel_id` | From your own configuration or from authenticated channel state, never from an unauthenticated payload field. With a signed payload, the signed value is authenticated; if you keep an allow-list, enforce it. |
| `contact_id` | The authenticated sender. It is also where replies are delivered and what erasure looks up. |
| `event_id` | The identity of ONE logical event. The provider's redelivery of the same event MUST reuse the same `event_id`; two different events MUST differ; it is never empty. Dedupe is `(tenant_id, channel_id, event_id)`: the first writer wins and the duplicate is ignored without comparing its content. A fresh random id per delivery duplicates turns; reusing one id for many events silently drops all but the first. Do not mint ids starting with `system:` (the runtime's own timers use them). |
| `conversation_id` | Unique per **tenant** (not per channel), deterministic and stable for the same conversation, and bound for good to one channel, one contact and one scope. Namespace it by channel (RelayPlane uses `{instance}:{contact}`; that is its scheme, not a rule). Reusing an id with another channel, contact or scope is refused with `ConversationIdentityConflictError` and the event is not stored. A new id per message fragments the state silently. |
| `scope` | Normally `None`: `receive` stamps the runtime's scope. Set it only from trusted configuration; it is read at first contact and can never change. |
| `session_id` | Required text. It is stored with the event, but the conversation keeps its FIRST value; later values change neither identity, dedupe nor order. Keep it stable (using the `conversation_id` is fine). |
| `text`, `media` | What the contact sent (`text` may be empty for media). |
| `kind` | `"user"` for everything a channel delivers. `"system"` is the runtime's own (proactive timers); never set it from channel input. |
| `occurred_at` | When the contact's event happened, best available (the provider's time if you have it, else your receipt time); timezone-aware. It orders events that have no sequence, decides "late", and feeds the last-activity time used by the channel service window. It is **not** confirmation evidence. There is no skew correction: a grossly wrong or future value skews those. |
| `received_at` | YOUR local receipt time of this delivery; timezone-aware; `receive` does not stamp it. Tie-break and latency only. |
| `source_sequence` | `None` unless the channel has a trustworthy sequence. Use it only when its values are meaningfully comparable (increasing, gaps are fine) among the events of one conversation that can be aggregated together. Never invent it from arrival order. Sequenced events sort ahead of unsequenced ones, so do not mix them in one pending stream unless that is intended. If the provider's sequence restarts or changes scope and events from both epochs can wait together, do not expose the values as comparable. When comparability cannot be guaranteed, send `None`. |
| `provider_occurred_at` | The PROVIDER's clock for this message, comparable with the `provider_accepted_at` that the same channel's sender reports; otherwise `None`. **Never copy `received_at`, local or database time into it**: that fabricates the proof that a "yes" came after the prompt. Absence is safe, invented evidence is not. |
| `reply_to_provider_message_id` | The provider's own statement that this event replies to that outbound message; `None` when the channel has no replies. Never invent it. |
| `provider_message_id` | The provider's id of this message; used so a deleted message that no turn claimed yet can be withdrawn. Not part of dedupe. |
| `trace_id` | Optional correlation id; observability only, never identity, dedupe or proof. If absent, the turn gets its own. |

**Order inside a conversation.** Events waiting together are aggregated by `source_sequence`
ascending (events without one come after all that have one), then `occurred_at`, then
`received_at`, then `event_id`. A late event (older than what the conversation already handled)
joins the next turn and never rewrites a finished one.

**Confirmation evidence.** A "yes" is provable by either (a) `reply_to_provider_message_id`
naming the accepted prompt, or (b) an accepted prompt with `provider_accepted_at` AND an inbound
`provider_occurred_at`, both in the same provider clock, more than 2 s apart (the tolerance is
fixed at 2 s today, it is not configurable). A channel that can offer neither still works for
everything else, but a protected action can never be confirmed through it.

How a "yes" is judged:

| Evidence | Result |
|---|---|
| replies to the prompt that was accepted | confirmed |
| replies to some other message | not an answer to the prompt |
| no reply-to, and the message is later than the prompt by more than 2 s | confirmed |
| no reply-to, within 2 s of the prompt's acceptance, or either timestamp missing | **unproven**: asked again, never executed |
| the prompt was never accepted by the channel (queued, sending, unknown) | not confirmed; a new prompt is sent |

The terminal channel (`ConsoleChannel`) provides all of it, which is why `serve` just works. A
channel with no timestamps and no reply-to can never confirm anything: this is deliberate.

**Telling the contact why.** When a clear "yes" is unproven, say so instead of "I did not
understand". Set it in the manifest (per language, with `{summary}`):

```yaml
confirmation:
  reprompt: "Não entendi. {summary}. Responda SIM para confirmar ou NÃO para cancelar."
  reprompt_unproven: 'Não consegui confirmar que o seu "sim" responde à minha pergunta. {summary}. Responda SIM ou NÃO respondendo diretamente à mensagem acima.'
```

Without `reprompt_unproven`, `reprompt` is used for both cases. The reason is also logged
(`conversation_agent.confirmation`: `confirmation not provable: eligibility=AMBIGUOUS ...`).

## 6. Tuning what the agent says and does

| You want | Manifest |
|---|---|
| a step-by-step collection with no LLM in the happy path | `flows:` (triggers, slots, steps) |
| a free-text slot that must not swallow questions | `question_check: model` on the slot |
| let the contact ask for a person | `human_request:` (`available: false` if nobody is behind the bot) |
| the exact words after a confirmed action succeeds (no model) | `executed_template` on the capability, e.g. `"Reserva {booking_id} confirmada para {start_at}."` (arguments and output fields; datetimes in the agent's timezone) |
| options that read well and pick more than one value | on a `choose` step: `label: "{court} - {start_at}"` (item fields; datetimes read as "ter 06/10 às 10:00") and `also: {court: court}` to fill other slots from the picked row |
| a part of the day ("à noite") narrowing the options | an enum slot (`morning`/`afternoon`/`evening`/`night` as canonical values) + `period_slot:` on the choose step; `period_hours:` redefines the hours |
| skip the question when one option is left | `auto_select_single: true` on the choose step |
| a question with a trigger word must not start a flow | `start_on_questions: false` on the flow (the agent answers the question instead) |
| a reply sent as several messages (answer, then the flow's question) | `split_replies: true` on the agent: one message per blank-line paragraph, at most 4 |
| the contact's id in your API | `send_contact_id: true` on the CONNECTION (operator-side, off by default): sent as `X-Contact-Id` |
| other languages for media and confirmations | `media:` and `confirmation:` texts |
| limits on cost and size | `max_tokens_per_turn`, `max_media_bytes`, LLM budgets |

Voice messages are transcribed when the agent says `transcription: on` AND you pass a `Transcriber`
(`transcriber=` to `Runtime.build`; see *Voice messages*); images, video and documents are named to the
agent, never described. A capability can take a file by handle: see *Passing a file to a tool*.

Two flows can never share a trigger phrase at the same priority (the compiler says `FLOW_TRIGGER_TIE`).
If different phrases both match one message with equal priority and specificity ("cancelar minha
reserva"), no flow starts and the runtime logs `flow trigger tie: a, b`; set `priority`.

## Voice messages (speech-to-text)

A voice message reaches the agent as text only when two things are both true: you (the operator) built a
transcriber, and the agent's manifest says `transcription: on`. The default is **off**: with `off` the
audio is only named to the agent (`media.audio_disabled`) and is never downloaded or sent anywhere. A voice
is personal data and the transcriber may be a third-party API, so neither side can turn it on alone.

```yaml
transcription: on        # in the agent manifest
```

```bash
# the operator side, in the environment (see .env.example)
STT_PROVIDER=groq        # or openai, or openai_compat for a local server
STT_API_KEY=...          # (groq/openai) falls back to GROQ_API_KEY / OPENAI_API_KEY
STT_LANGUAGE=pt
```

```python
from conversation_agent.app.stt_factory import STTConfig, build_transcriber

config = STTConfig.from_env(os.environ)  # None when STT_PROVIDER is unset: no transcriber
transcriber = build_transcriber(config, media_fetcher, usage=usage_store) if config else None
runtime = Runtime.build(..., transcriber=transcriber)
```

One adapter speaks the OpenAI-compatible `POST /audio/transcriptions` protocol, so the choice is a
configuration, not code:

| You want | Configure |
|---|---|
| Groq (hosted, same key as the LLM) | `STT_PROVIDER=groq` (model `whisper-large-v3-turbo`) |
| OpenAI | `STT_PROVIDER=openai` (model `whisper-1`) |
| Audio that never leaves your infrastructure | `STT_PROVIDER=openai_compat`, `STT_BASE_URL=http://localhost:8000/v1`, `STT_MODEL=<the server's model>`, no key: any server that implements the protocol (faster-whisper server, whisper.cpp server, LocalAI, vLLM) |

Notes: WhatsApp voice notes are ogg/opus (the adapter names the file from the MIME type, since servers
decide the format from the name; a local server that cannot read ogg needs conversion on its side). The
size limit is `STT_MAX_AUDIO_BYTES` (default 25 MiB, what hosted endpoints accept) and the audio is
fetched with that bound. A failed or empty transcription never fails the turn: the agent is told the
message could not be transcribed. A replayed turn reuses the stored text and is never billed twice.

Cost: every transcription is written to the usage books by the **second of audio** (never the audio or the
text), under purpose `transcription`; give the model an `audio_per_minute` price in `ops/llm_prices.yaml`
or its calls are reported as unpriced. The `Transcriber` port is also the place to plug something else
entirely (a different provider, an in-house service): implement `transcribe(media, context)`.

## Writing your own ToolProvider

`HTTPToolProvider` and `MCPToolProvider` are the first-party providers; anything else implements
`ToolProvider` (`ports/tool_provider.py`; normative text in ADR-024 and `RUNTIME_PROTOCOL.md` §9).
Everything you need is below; you do not have to read the adapters, the runner or the ledger.

1. **Identity.** `context.invocation_id` is the one identity of a logical tool call. The runtime
   passes the same value on the first attempt, every technical retry, every reconciliation resend
   and after a restart. Do not mint your own per attempt.
2. **External idempotency.** For a non-read tool with `idempotency_supported`, bind that identity
   to the external system's dedupe/replay mechanism (header, request token, native field: your
   choice). If the target cannot take the literal value, derive a deterministic encoding of it;
   never add per-attempt randomness. Declaring `idempotency_supported` asserts the target really
   dedupes for every resend the tool's `retry` and `recovery` may perform, including a
   reconciliation resend after its backoff; the framework cannot check that, so declare it only
   when it is true.
3. **Truthful result.** Return a `ToolResult`; do not raise. For a write: the effect provably did
   not happen -> `technical_error` (`retryable=True` only then; it means "this attempt had no
   effect", not "the network failed"); a known domain refusal -> `business_error` /
   `validation_error` / `policy_denied`; the request may have arrived and you cannot prove the
   effect -> `unknown`, always, even if the target supports idempotency. A timeout after sending,
   a reset, or an unusable answer to a write is `unknown`, never `technical_error` or `timeout`.
   The same rule binds a custom `error_map`.
4. **Retry.** The runner retries a `technical_error` with `retryable=True`; for a write only when
   the tool is `idempotency_supported` with `retry.mode: same_idempotency_key` (the compiler
   refuses any other write retry).
5. **Unknown.** The runner never retries `unknown`. The reconciliation worker handles it with the
   `recovery` frozen when the call was prepared: `retry_same_key` (same identity),
   `status_lookup`, or `human_handoff` (the default for a write).

If `execute` raises anyway, the runtime treats a write as `unknown` and a read as a non-retryable
`technical_error`; that is a safety net, not part of the API. A wrapper around a provider (breaker,
metrics) must pass the inner classification through unchanged. A provider that classifies an
ambiguous write as `technical_error`/`timeout`, claims `retryable` without proof, or mints a new
identity per retry breaks INV-006 and INV-021.

## Passing a file to a tool

A capability can take a file the contact sent (a picture of the problem, a receipt) with a field of
`type: media`. The contact's files get **handles** (`media_1`, `media_2`, ...), the model passes one, and the
runtime fills in the file's reference. The model never sees or writes ids, checksums or bytes.

```yaml
capabilities:
  - name: files.attach
    risk: irreversible
    confirmation_required: true
    summary_template: "Attach {file} to ticket {ticket}"     # {file} reads as the file's name
    input:
      ticket: {type: string}
      file: {type: media, description: The file to attach}
tools:
  - name: tickets_attach
    provider: http
    http:
      method: POST
      path: /tickets/{ticket}/files
      body: [file]                 # a file argument must be a body field
      media_content: [file]        # optional: ALSO send the bytes (base64) ...
    input: {ticket: {type: string}, file: {type: media}}
bindings:
  - capability: files.attach
    tool: tickets_attach
    input_map: {ticket: $.ticket, file: $.file}
```

What happens: when a message carries files, an agent that has such a capability sees `[image received]
[media_2]` in the text, so it knows what to pass. The tool receives
`{"handle", "media_id", "kind", "mime_type", "size_bytes", "sha256", "filename"}`; with `media_content` it also
gets `content_base64`, fetched at send time (needs `media_fetcher=` on `HTTPToolProvider`, bounded by the
connection's request size and checked against the file's checksum). Without `media_content` the tool only
learns who the file is, which is enough for a service that can fetch it from the gateway itself.

Rules worth knowing:

- A handle only works in the conversation it came from. The conversation keeps the **10 newest** files
  (handles are never reused: a file that ages out cannot be attached any more) and they live and die with the
  conversation (retention and erasure remove them).
- The reference, including the checksum, is part of the arguments, so a confirmation covers THAT file.
- An unknown handle, a channel id, or an object written by the model is refused and nothing is proposed.
- Only an HTTP tool can take a file, and only in its body (the compiler checks).
- Agents with no `media` input see media exactly as before (no handles).

**Testing voice in evals.** A suite turn can say `voice: "quero falar com um atendente"`: the harness
sends a voice message whose transcript is exactly that (no audio, no provider), so a suite proves what the
agent does with the words AND that an agent with `transcription: off` ignores them.

## 7. When something does not work

| Symptom | Likely cause |
|---|---|
| the agent keeps asking to confirm | no reply-to / timestamps from the channel, or the contact answered within 2 s (§5); check the `confirmation not provable` log line |
| the reply never arrives | the outbox row is pending: is a worker running (`run`/`tick`) and does your sender return `ACCEPTED`? |
| `ImportError ... conversation-agent[postgres]` | install the extra |
| `SchemaOutOfDateError` / `SchemaAheadError` / `MigrationHistoryError` / `MigrationDriftError` from `ensure_schema_current` | `python -m conversation_agent.app.migrate status` (read-only), then `apply` only for out of date; ahead, gap or drift: see OPERATIONS "Schema migrations" |
| a tool call is refused | the capability is not in `allowed_capabilities`, or the connection id has no provider |
| the agent says it cannot see an image | by design: media is only named, not read |
| voice messages arrive as "this assistant does not process voice messages" | `transcription` is `off` (the default) for that agent: set `transcription: on` and give `Runtime.build` a transcriber |
| voice arrives as "could not be transcribed" | the agent asked for it but the operator built no transcriber, or the provider failed (check the `outbox`/usage books for purpose `transcription` errors) |

**Secrets you must provide.** `PSEUDONYM_KEY` (>= 32 random bytes; `app.wiring.configure_pseudonyms_from_env`
or `core.models.audit.configure_pseudonym_key`) keys the references written by erasure and ownership
audit. Without it they refuse to produce one. Rotating it changes every reference from then on: keep the
old key if you must prove an earlier erasure.

**Auditing ownership.** An operator's `OwnershipService` writes its audit entry in the same transaction as the
change. To also audit the bot's own handoffs, `Runtime.build(audit_ownership=True)` (needs `PSEUDONYM_KEY`).

**LLM budget.** Give a tenant a budget (`PostgresBudgetStore.set`) and pass the gate to the runtime
(`Runtime.build(budget=BudgetEvaluator(budgets, usage, prices))`). Over the limit, the default policy `degrade`
tells the contact once per period (`budget_exceeded_reply` in the manifest, in the agent's language) instead of
calling the model or dropping the message; `alert` and `refuse_new` are the alternatives. See *llm-budget* in
`OPERATIONS.md`.

## 8. Operating it

`app.migrate` (schema), `app.ops check|render` (SLOs, alerts, dashboard; `ops/validate_artifacts.py` checks the
generated alerts and dashboard with `promtool` and a real Grafana), `app.integrity`,
`app.usage` (LLM cost and budgets) and the runbook in [`OPERATIONS.md`](./OPERATIONS.md). Release
and rollback of agent versions are described there too. The framework has no canonical deployment: hosting, backups and
observability backends are the embedding application's choice (`OPERATIONS.md` separates framework contract from operator
responsibility; ADR-022). Before a production pilot, validate the
RelayPlane contract against the deployed gateway and keep the evidence: [`RELEASE_EVIDENCE.md`](./RELEASE_EVIDENCE.md).
