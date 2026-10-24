# reference-tool-selector

A minimal **external tool-ranking server** for the `http` provider of tool selection (ADR-029,
`docs/TOOL_SELECTOR_CONTRACT.md`). It ranks tools by the word stems a message shares with each
tool's name and description: naive on purpose, so it runs anywhere. A real server puts a
classifier or embeddings behind the same `POST /tool-selection`.

```
uv run uvicorn tool_selector_api.main:app --port 8002          # open
SELECTOR_TOKEN=secret uv run uvicorn tool_selector_api.main:app --port 8002   # needs Authorization: Bearer secret
```

Measure it with the evaluation runner:

```
uv run python -m conversation_agent.app.tool_selection_eval \
  --catalog examples/tool-selection-eval/catalog.json \
  --dataset examples/tool-selection-eval/heldout.jsonl \
  --provider http --url http://127.0.0.1:8002 [--token-env SELECTOR_TOKEN]
```

`conversation_agent` never imports this package (import-linter).
