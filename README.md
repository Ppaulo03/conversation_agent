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
export ANTHROPIC_API_KEY=...        # PowerShell: $env:ANTHROPIC_API_KEY = "..."
uv run python -m vertical_slice
```

## Checks

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src examples
uv run lint-imports
uv run pytest
```
