# AGENTS.local.md — conversation_agent

Deltas do repositório sobre o `AGENTS.md` global.

## Stack e comandos

- Python >=3.12 (testado 3.13), `uv`, Pydantic v2, httpx, FastAPI/uvicorn (só a API de referência), pytest-asyncio.
- Setup: `uv sync` · Testes: `uv run pytest` · Lint: `uv run ruff check . && uv run ruff format --check .`
- Tipos: `uv run mypy src examples` · Fronteiras: `uv run lint-imports`
- API de referência: `uv run uvicorn scheduling_api.main:app --port 8001` · CLI: `uv run python -m vertical_slice`
- Teste live com orçamento diário (`tests/support/live_budget.py`, `LIVE_LLM_DAILY_CALLS`=20, estado em `.live_llm_usage.json`
  git-ignored; 429 zera o dia). Não rodar em loop: o Groq tem limite diário baixo. Modelo Groq vigente: `openai/gpt-oss-120b`
  (`llama-3.3-70b-versatile` retornou 404 nesta conta; listar com `GET /openai/v1/models`).
- Testes live (explícitos): `uv run pytest -m integration` (`uv run --env-file .env pytest -m integration`; precisa de provider configurado).
- LLM provider-agnóstico via env: `LLM_PROVIDER` (groq|openai|openai_compat|anthropic), `LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL`;
  `GROQ_API_KEY`/`ANTHROPIC_API_KEY` valem como fallback. Resolução em `app/llm_factory.py`. `.env` é git-ignored e nunca deve ser lido/impresso.
- Postgres local: `docker compose up -d` (`docker-compose.yml`, credenciais dev em `.env.example`).
- Docs normativos em `docs/` (sem sufixo `_v4`). Progresso e DoD em `IMPLEMENTATION_STATUS.md`.
- Repo git inicializado (`main` = baseline da Fase 1). Trabalho em `feature/*`; nada direto em `main`.

## Aprendizados acumulados

- Nomes de capability são validados (dominio.acao, lower_snake, sem `__`) para o nome LLM (.→`__`) ser injetivo.
- Modelos de resultado impõem invariantes (success⇔sem error; falha⇔error sem data): construa ToolResult/CapabilityResult coerentes.
- Falha antes de qualquer I/O = technical_error; só ambiguidade pós-envio de write vira unknown.

- Nunca criar `__init__.py` com `Set-Content -Encoding utf8` no PowerShell 5.1: grava BOM e o `ast.parse` quebra.
  Use `[System.IO.File]::WriteAllBytes(path, [byte[]]@())`. Idem para ler/regravar arquivos com acentos: não use
  `Get-Content` sem `-Encoding UTF8` (corrompe UTF-8).
- `tests/` é colocado no `sys.path` pelo pytest (rootdir/conftest); helpers ficam em `tests/support` e são importados
  como `support.*`; `conftest` é importável como módulo.
- `src/` e `examples/*` entram no path via `dev-mode-dirs` do hatch (install editável); `examples/` nunca pode ser
  importado por `src/` (contrato do import-linter + teste de arquitetura).
- Escrita não-read com timeout/5xx é `unknown` mesmo que o `error_map` diga `technical_error`: coerção em
  `tools/error_mapping.py` (INV-006/INV-021).

- Postgres: `asyncpg` (o psycopg async não roda no ProactorEventLoop do Windows). Testes em `tests/postgres` usam o banco
  `conversation_agent_test` (criado/migrado/truncado pelos fixtures; `docker compose up -d` antes). Credenciais via
  `POSTGRES_*` ou defaults do compose.
- Fencing de conversa = `SELECT ... FOR SHARE` condicionado a (owner, epoch) no começo de toda UoW; o takeover exige lock
  exclusivo da mesma linha. Ledger usa só `execution_epoch` e nunca toca a linha da conversa (INV-011/016).
- `SimulatedCrash` é `BaseException` de propósito: nenhum `except Exception` pode engolir um "kill" nos testes de chaos.
- Cada caso de chaos exigido tem um `test_Cxx_*` nomeado; `tests/architecture/test_chaos_gates.py` falha se faltar algum.
- PowerShell: `R` é alias de `Invoke-History` (não use como nome de função); `.Replace()` multilinha falha em arquivos
  CRLF — use a ferramenta Edit; `;` não interrompe um `git commit` após teste vermelho (encadeie com `if ($?)`).
- Confirmação: `TRUNCATE` dos fixtures precisa listar toda tabela nova (uma `pending_actions` vazando entre testes sequestra o turno
  seguinte). Um turno retomado deve voltar ao estágio já journalado (ver `find_step(CONFIRMATION_DECISION)` no coordinator).
- `httpx.AsyncClient.send` herda `follow_redirects` do client injetado: sempre passe `follow_redirects=False` no provider.
- PowerShell 5.1: `String.Replace` não aceita 3 argumentos e `"\n"` entre aspas duplas é literal; use `` `n `` ou a ferramenta Edit.
