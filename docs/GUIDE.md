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

db = await PostgresDatabase.connect(DATABASE_URL)  # migrate first (app.migrate)
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

**Several agents in one database.** Each runtime handles only the conversations of its *scope*
(default: its agent's id; override with `Runtime.build(scope=...)`). The scope is fixed when a
conversation is first seen: `Runtime.receive` stamps it, and a webhook takes it from the
subscription (`Subscription.scope`), never from the payload. A runtime never claims another
scope's turns or outbox messages, and a conversation cannot move between scopes. Conversations
pinned to an agent before scopes existed were assigned to that agent's scope by the migration; any
other legacy conversation (no scope) is adopted by the first scoped event that arrives for it and
is invisible to scoped runtimes until then. Several workers *of the same scope* share the work as
below.

If your channel can tell you later what became of a send (a gateway that answers `queued` and then
settles it), give `Runtime.build` a `delivery_policy` (how long the channel remembers an idempotency
key, and how long a send may be retried) and make your sender implement `lookup(message)`: the
Runtime then reconciles unsettled sends by asking the channel, never by sending again.

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

**Each inbound event** (`InboundEvent`) should carry as much as the channel knows:

| Field | Why |
|---|---|
| `event_id` | dedupe identity, unique per (tenant, channel) |
| `reply_to_provider_message_id` | the strongest proof: "this answers message X" |
| `provider_occurred_at` | the channel's time of the message; compared with `provider_accepted_at` |

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

Voice messages are transcribed when you pass a `Transcriber` (`transcriber=` to `Runtime.build`);
images, video and documents are named to the agent, never described. Tools do not receive media yet.

Two flows can never share a trigger phrase at the same priority (the compiler says `FLOW_TRIGGER_TIE`).
If different phrases both match one message with equal priority and specificity ("cancelar minha
reserva"), no flow starts and the runtime logs `flow trigger tie: a, b`; set `priority`.

## 7. When something does not work

| Symptom | Likely cause |
|---|---|
| the agent keeps asking to confirm | no reply-to / timestamps from the channel, or the contact answered within 2 s (§5); check the `confirmation not provable` log line |
| the reply never arrives | the outbox row is pending: is a worker running (`run`/`tick`) and does your sender return `ACCEPTED`? |
| `ImportError ... conversation-agent[postgres]` | install the extra |
| `schema ... pending/drift` | `python -m conversation_agent.app.migrate status`, then `apply` |
| a tool call is refused | the capability is not in `allowed_capabilities`, or the connection id has no provider |
| the agent says it cannot see an image | by design: media is only named, not read |

**Secrets you must provide.** `PSEUDONYM_KEY` (>= 32 random bytes; `app.wiring.configure_pseudonyms_from_env`
or `core.models.audit.configure_pseudonym_key`) keys the references written by erasure and ownership
audit. Without it they refuse to produce one. Rotating it changes every reference from then on: keep the
old key if you must prove an earlier erasure.

## 8. Operating it

`app.migrate` (schema), `app.ops check|render` (SLOs, alerts, dashboard), `app.integrity`,
`app.usage` (LLM cost and budgets) and the runbook in [`OPERATIONS.md`](./OPERATIONS.md). Release
and rollback of agent versions are described there too. Before a production pilot, validate the
RelayPlane contract against the deployed gateway and keep the evidence: [`RELEASE_EVIDENCE.md`](./RELEASE_EVIDENCE.md).
