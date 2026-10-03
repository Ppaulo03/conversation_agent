# AGENTS.local.md — conversation_agent

Deltas do repositório sobre o `AGENTS.md` global.

## Stack e comandos

- Python >=3.12 (testado 3.13), `uv`, Pydantic v2, httpx, FastAPI/uvicorn (só a API de referência), pytest-asyncio.
- Setup: `uv sync` · Testes: `uv run pytest` · Lint: `uv run ruff check . && uv run ruff format --check .`
- Tipos: `uv run mypy src examples` · Fronteiras: `uv run lint-imports`
- API de referência: `uv run uvicorn scheduling_api.main:app --port 8001` · CLI: `uv run python -m vertical_slice`
- Testes live (explícitos): `uv run pytest -m integration` (precisa `ANTHROPIC_API_KEY`).
- Docs normativos em `docs/` (sem sufixo `_v4`). Progresso e DoD em `IMPLEMENTATION_STATUS.md`.
- O repositório não é um repo git ainda; GitFlow do AGENTS.md global vale quando for inicializado.

## Aprendizados acumulados

- Nunca criar `__init__.py` com `Set-Content -Encoding utf8` no PowerShell 5.1: grava BOM e o `ast.parse` quebra.
  Use `[System.IO.File]::WriteAllBytes(path, [byte[]]@())`. Idem para ler/regravar arquivos com acentos: não use
  `Get-Content` sem `-Encoding UTF8` (corrompe UTF-8).
- `tests/` é colocado no `sys.path` pelo pytest (rootdir/conftest); helpers ficam em `tests/support` e são importados
  como `support.*`; `conftest` é importável como módulo.
- `src/` e `examples/*` entram no path via `dev-mode-dirs` do hatch (install editável); `examples/` nunca pode ser
  importado por `src/` (contrato do import-linter + teste de arquitetura).
- Escrita não-read com timeout/5xx é `unknown` mesmo que o `error_map` diga `technical_error`: coerção em
  `tools/error_mapping.py` (INV-006/INV-021).
