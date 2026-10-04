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

- (Fase 4) Em PowerShell `R` é alias de `Invoke-History`: nunca nomeie helper de replace como `R`. `String.Replace` com 3 args falha em silêncio e o arquivo é gravado sem a edição.
- (Fase 4) `ConversationState` ganhou `flows`; `test_runtime_does_not_own_business_state` lista os campos permitidos (só dado conversacional).
- (Fase 4) Coluna `state_json` (jsonb) volta como dict/str conforme o codec: nos testes use `json.loads` se for `str`.
- (Fase 4) Cancelamento: `cancel_requested` precisa ser reconhecido localmente (`LeaseHandle.acknowledge_cancel`) ao completar/cancelar o turno; senão o turno seguinte herda o pedido do heartbeat e é cancelado à toa (e reabrir os mesmos eventos colidiria no PK de `turns` sem o sal do `turn_id`).
- (Fase 4) Gatilho vs resposta: "a consulta" respondendo "qual serviço?" contém o trigger "consulta"; a resposta ao flow ativo tem precedência sobre trigger de outro flow.
- (Fase 4.1) Regra global validada localmente é bug esperado: "é protegido?" vive em `ResolvedToolBinding.requires_protection`; qualquer validação nova deve resolver o binding, nunca olhar `capability.risk`/`tool.risk` isolados. Em testes, use `rebuild(agent, ...)` (reconstrói e revalida); `model_copy` pula validação.
- (Fase 5) JSONB não preserva a ordem das chaves: tudo que é comparado/hasheado depois de um round-trip pelo banco deve ser independente de ordem (ex.: `required` do JSON Schema é ordenado em `schema_fingerprint`). Persistir manifest com `model_dump(exclude_unset=True)` (defaults "honrados só se escritos", como `Ref.default`).
- (Fase 5) YAML 1.1 lê `on:` como booleano: o loader converte chaves `True/False` de volta para `on/off`. Texto com apóstrofo em here-string de PowerShell com aspas simples quebra o script inteiro (nada é gravado): use a ferramenta Write para arquivos com código.
- (Fase 5) `model_copy` não revalida: em testes de definição use `rebuild(...)` (reconstrói `AgentDefinition`). `ReconciliationWorker` agora tem modo single-version (`pipeline=`) e versionado (`registry=`+`agent_id=`+`pipeline_factory=`).
- (Fase 5.1) O runtime recebe `CompiledAgent` (selado). Testes que montam `TurnEngine` direto usam `compile_agent(agent)`. Tipos de mapping: ao criar um transform novo, registre-o em `TRANSFORM_SPECS` (entrada→saída) e em `tools.mapping.TRANSFORMS` (um teste mantém os nomes sincronizados).
- (Fase 5.2) `CapabilityPipeline` recebe `CompiledAgent`; em testes use `support.builders.make_pipeline(agent, gate, runner)` (compila antes). `build_engine(pipeline=...)` ignora `flows=`: o pipeline fixa o agente (`world.pipeline(..., flows=True)` para testes com Flow). O registry é tenant-scoped (`TENANT = IDENTITY.tenant_id` nos testes). Pydantic não valida/coage defaults: coagir com `TypeAdapter` ao construir o modelo.
- (Fase 5.3) Compiler e runtime executam a MESMA linguagem de mapping: qualquer regra nova em `tools/mapping.py` (cardinalidade de `[*]`, tipo de `default`, enum só em string) precisa do espelho em `core/compiler_types.py`/`compiler.py` e de um teste que prove os dois lados. O output mapping lê a resposta VALIDADA da Tool (`model_dump(mode="json")`), nunca o JSON cru.
- (Fase 5.4) Tempo: `Clock` = conversacional; `CoordinationClock` (`CoordinationTime(db)` em produção, `FixedCoordinationTime(clock)` nos testes, via `world.coord`) = lease/claim/backoff. Nunca compare `clock.now()` com um timestamp que o banco gerou. Para provar defesas de runtime contra definições que o compiler já recusa, use `make_pipeline(..., unchecked=True)`. Tipos do compiler: `("nullable", T)`, `("union", {…})`, `NONE`; `unify` nunca devolve `any` para tipos diferentes.
- (Fase 6) O RelayPlane real (`github.com/Ppaulo03/RelayPlane`, `docs/CONTRACT.md`) é a fonte do contrato (`docs/RELAYPLANE_CONTRACT.md` resume): envio assíncrono (202 QUEUED + `message_id`), SEM consulta por chave (reenviar a mesma chave dentro de `/limits.idempotency_retention_seconds` é o replay seguro; depois, duplica), UNKNOWN do gateway só sai por `resolve` humano. Nunca invente contrato de sistema do usuário: leia o repositório dele. Antes de produção rode `tests/contracts/test_relayplane_live.py` contra um gateway implantado. Em PowerShell, o scanner do harness bloqueia comandos cujo texto contém a palavra `del` (alias de Remove-Item): grave código com a ferramenta Write, não com here-string. Testes de entrega: `world.outbox_worker(..., sender=, retry_horizon=)`, `world.outbox_reconciler`, fixture `relay` (relógio do gateway controlável, `RelayHandle.advance`).
- (Fase 7) O transporte de tools sai de UM lugar, `adapters/tools/guard.py` (`GuardedTransport`); HTTP e MCP o compartilham: nunca duplique SSRF/pinning. Acrescentar campo a `ToolDefinition`/`ErrorMap` muda o digest do agente e o `binding_fingerprint` de intenções já preparadas: inclua-o no documento/fingerprint SÓ quando presente (como `mcp` e `error_map.tool_error`). Em testes com `LiveServer` compartilhado, uma falha injetada com `delay` deve registrar o efeito ANTES de dormir, senão a chamada tardia vaza para o teste seguinte. Heredoc bash com texto longo e aspas quebrou o parse da ferramenta: grave arquivos com Write.
- (Fase 8) Pack = dado instalado ANTES do compilador (`core/packs.py`), nunca lido pelo runtime; o vocabulário de domínio (agendamento etc.) vive em `packs/` e nos exemplos, nunca em `src/` (o teste de arquitetura barra). Um segundo sistema externo de vocabulário diferente (`examples/reference-agenda-api`) é a melhor forma de achar suposição escondida no framework: achou o bug do Flow que só casava o horário pedido com as 5 opções exibidas. Campo novo em `AgentManifest` precisa ser omitido quando não usado (`manifest_digest` usa `exclude_unset`). Com `LiveServer`/fixtures compartilhadas, cada API de exemplo tem seu handle (`api`, `agenda`) e o teste confere que a outra API NÃO recebeu requisições.
- (Fase 9) Um domínio novo é manifest + allowlist + bindings (sem tocar em `src/`); se algo no framework precisar mudar, isso é um ACHADO a registrar em `IMPLEMENTATION_STATUS.md` (e corrigir em commit separado se for defeito), não um ajuste silencioso. O gate "sem alterar core/engine" mede-se com `git diff main -- src/conversation_agent/{core,ports,tools,engine}`. Slot de texto livre engole a próxima mensagem (inclusive perguntas): ao desenhar Flows prefira slots estruturados antes de texto livre quando houver digressão esperada.
- (Fase 10) Operações administrativas novas devem (a) gravar `admin_audit` na MESMA transação (`insert_audit(conn, ...)`), com identificadores/contagens e nunca conteúdo, (b) usar o relógio do banco (`clock_timestamp()`), nunca `now()` do worker. Toda tabela nova entra na lista de TRUNCATE de `tests/postgres/conftest.py`; ao comparar idades no teste, calcule a data a partir de `clock_timestamp()` no próprio SQL (o relógio fixo dos testes está no futuro). `EXTRACT(EPOCH ...)` volta como Decimal no asyncpg: faça `::float8`. Artefatos operacionais (`ops/*.yml|json`) são GERADOS de `ops/slo.yaml` (`app.ops render`); `app.ops check` roda no CI. Nova migration muda o conjunto que `Migrator.ensure_current` exige: rode `uv run python -m conversation_agent.app.migrate check` no ambiente antes de subir.
- (Fase 11) Observabilidade: log com `log.<nível>("dominio.evento", extra={"fields": {...}})` (nome de evento estável, campos escalares; o contexto — tenant, turno, versão, trace — vem de `core.observability.bind`, nunca de argumento). Campo novo de contexto exige entrar em `FIELDS` (conjunto fechado: nunca um campo de conteúdo). Toda chamada ao LLM passa por `TurnEngine.llm_step(..., purpose=...)`; um novo chamador de modelo DEVE dar a sua finalidade. Preço de modelo mora em `ops/llm_prices.yaml` (vazio de propósito). Instrumentar com `span("nome")`, nunca com `time.time()` solto. Métrica nova: adicionar em `prometheus.py` (+ `available_metrics`) e, se for alertar, em `ops/slo.yaml` + seção do runbook, depois `app.ops render`. Em testes que checam o histograma, os buckets são cumulativos e `+Inf` == contagem.
