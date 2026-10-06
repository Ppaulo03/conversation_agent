# conversation_agent

Framework for stateful conversational agents. **It owns conversational state, not business
state.** **Start with the [usage guide](docs/GUIDE.md).** Architecture lives in [`docs/`](docs/README.md); implementation progress in
[`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md).

## Install

The base install is light (pydantic, httpx, pyyaml): compile manifests, run evals, build an
in-memory engine. Add what you use:

```bash
pip install "conversation-agent[postgres]"   # durable runtime: Runtime, serve, migrate, ops (asyncpg)
pip install "conversation-agent[anthropic]"  # the Anthropic provider (Groq/OpenAI-compatible need nothing)
pip install "conversation-agent[otel]"       # optional OTLP/HTTP trace export
pip install "conversation-agent[all]"
```

Composition helpers that need no extra: `conversation_agent.app.wiring` (`load_agent`,
`local_http_provider`, `build_memory_engine`). A missing extra is reported with the extra's name.

## Setup (development)

```bash
uv sync
```

## Run the Phase 1 vertical slice

```bash
# terminal 1: the external reference scheduling API
uv run uvicorn scheduling_api.main:app --port 8001

# terminal 2: the CLI agent (real provider)
# configure LLM_PROVIDER / LLM_API_KEY (or GROQ_API_KEY ...) in .env, see .env.example
uv run --env-file .env python -m vertical_slice
```

## Run the durable runtime (serve)

The full runtime (PostgreSQL, confirmation of protected actions, outbox, timers, reconciliation)
with the terminal as the channel, so a reservation is proposed, confirmed and really executed:

```bash
docker compose up -d
export DATABASE_URL=postgresql://conversation_agent:conversation_agent_dev@127.0.0.1:5432/conversation_agent
uv run python -m conversation_agent.app.migrate apply
uv run uvicorn scheduling_api.main:app --port 8001        # terminal 2: the external API
uv run --env-file .env python -m conversation_agent.app.serve     examples/vertical-slice/vertical_slice/agent.yaml --http scheduling_api=http://127.0.0.1:8001
```

`--http ID=URL` gives the base URL of the connection the manifest calls `ID` (local development
only). In your own deployment, build the same thing with `conversation_agent.app.runtime.Runtime`
and your channel's sender: `Runtime.build(db=..., compiled=..., llm=..., providers=..., sender=...)`,
then `receive(event)` for each inbound event and `run(stop)` (or `tick()`) to drive it.

A confirmation ("yes") must be provably a reply to the prompt that was sent: the channel gives the
sender's `provider_accepted_at` and each inbound event's `provider_occurred_at` and/or
`reply_to_provider_message_id`. Without either, a bare "yes" is answered with a new question, never
executed (INV-022). The terminal channel supplies both.

## Local services (Docker)

```bash
docker compose up -d      # PostgreSQL 16 on 127.0.0.1:5432 (used from Phase 2); see .env.example
docker compose down       # add -v to drop the data volume
```

## Checks

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src examples
uv run lint-imports
uv run pytest
```
