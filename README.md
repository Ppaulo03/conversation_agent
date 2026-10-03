# conversation_agent

Framework for stateful conversational agents. **It owns conversational state, not business
state.** Architecture lives in [`docs/`](docs/README.md); implementation progress in
[`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md).

## Setup

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
