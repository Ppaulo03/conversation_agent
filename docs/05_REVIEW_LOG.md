# 05 — Review log

O que está **tecnicamente em aberto**. Lifecycle e severidades da [metodologia §17–18](./01_METHODOLOGY.md):

- Status: `OPEN` · `PARTIALLY_RESOLVED` · `RESOLVED` · `ACCEPTED_RISK` · `SUPERSEDED`
- Severidade: `BLOCKER` · `HIGH` · `MEDIUM` · `LOW` · `NICE_TO_HAVE`

Regra de uso: toda review começa por este arquivo (evita “review amnesia”). Um finding só vira `RESOLVED` com a evidência da
resolução anexada; `ACCEPTED_RISK` exige decisão explícita do Product Owner registrada aqui.

## Baseline executado — 2026-10-07

Ambiente: Windows 11, Python 3.13.1 (`.venv` do projeto, sem `uv sync` nem alteração de dependências), PostgreSQL 16 em Docker
(`conversation_agent_postgres`, já em execução), base `a9c7af9` (`main`) mais somente arquivos `.md`. Logs brutos ficaram fora do repositório (scratchpad da sessão).

| Gate | Comando | Resultado | Evidência / notas |
|---|---|---|---|
| Lint | `python -m ruff check .` | **PASS** | `All checks passed!` |
| Formato | `python -m ruff format --check .` | **PASS** | `360 files already formatted` |
| Tipos (comando oficial do projeto) | `python -m mypy src examples` | **PASS** | `Success: no issues found in 218 source files` (o comando de `README.md`/`AGENTS.local.md`) |
| Tipos (`mypy .`, fora do gate) | `python -m mypy .` | **FAIL (informativo)** | 268 erros em 87 arquivos, todos em `tests/` (imports `support.*`/`conftest`/`postgres.world` não resolvidos pela configuração, anotações). Não é gate oficial (a configuração só declara `src` e `examples`); ver F-008 (RESOLVED) |
| Fronteiras | `lint-imports` | **PASS** | `Contracts: 9 kept, 0 broken.` |
| Suíte padrão (inclui PostgreSQL, chaos, invariantes, arquitetura, evals, contratos) | `python -m pytest -rs -q -p no:cacheprovider` | **PASS** | `1649 passed, 8 deselected in 414.30s`; zero skips. `tests/postgres` (435 testes) rodaram contra o PostgreSQL real; `tests/architecture` (229) inclui `test_chaos_gates.py` (C01–C16 exigidos) |
| Artefatos operacionais | `python -m conversation_agent.app.ops check` | **PASS** | `ok` (ops/ em dia com `ops/slo.yaml`) |
| Contrato RelayPlane real | `pytest tests/contracts/test_relayplane_live.py -m integration -rs` | **NOT_VERIFIED** | 3 skipped: variáveis `RELAYPLANE_*` ausentes do ambiente. Ver F-002 |
| Testes live com LLM | `pytest -m integration` (5 testes em `tests/integration/`) | **NOT_VERIFIED (opt-in)** | Não executados de propósito: exigem credenciais, consomem o orçamento diário do provedor e enviam dados a um serviço externo. Não condicionam o baseline do repositório |
| Migrations contra um banco de ambiente | `python -m conversation_agent.app.migrate check` | **NOT_VERIFIED (ambiente)** | `DATABASE_URL` não definido; o comportamento do `Migrator` é coberto por `tests/postgres/test_migrator.py` (PASS), mas o banco de dev não foi checado |
| Restore/DR | — | **NOT_VERIFIED (consumidor)** | Backup/restore é do operador (ADR-022); o contrato do framework pós-restore está em `OPERATIONS.md`; ver F-003 (SUPERSEDED) |
| CI do repositório | — | **NOT_VERIFIED** | Não existe arquivo de pipeline no repositório; ver F-007 |

Os seis primeiros gates `PASS` constituem o **baseline do repositório** (F-001). Os `NOT_VERIFIED` são evidência externa/operacional e estão rastreados em F-002 e F-007 (do framework); após a ADR-022, F-003 e F-005 foram reclassificados como responsabilidade do consumidor (F-009 cobre o único resíduo do framework). `migrate check` de um deployment concreto e os testes live com LLM são evidência de ambiente / opt-in. `PASS` não prova o contrato do gateway, restore nem operação em produção.

## Resumo

| ID | Severidade | Status | Título |
|---|---|---|---|
| F-001 | MEDIUM | RESOLVED | Baseline verificável do repositório |
| F-002 | HIGH | PARTIALLY_RESOLVED | Evidência do contrato RelayPlane real (sandbox registrado; falta o gateway implantado com número real) |
| F-003 | HIGH | SUPERSEDED | Ensaio de disaster recovery (ownership do consumidor, ADR-022) |
| F-004 | MEDIUM | RESOLVED | Deriva de autoridade da documentação |
| F-005 | MEDIUM | SUPERSEDED | Lacunas de observabilidade operacional (backend é do consumidor, ADR-022; resíduo → F-009) |
| F-006 | MEDIUM | RESOLVED | Entrypoint de produção com RelayPlane (resolvido por ADR-022: não existir é intencional) |
| F-007 | MEDIUM | PARTIALLY_RESOLVED | Sem pipeline de CI no repositório (pipeline escrito; falta a primeira execução verde no GitHub) |
| F-008 | LOW | RESOLVED | `mypy .` fora do escopo configurado |
| F-009 | LOW | RESOLVED | Shipped observability artifacts lack target-format compatibility evidence |
| F-010 | HIGH | RESOLVED | Generic delivery safety contract is incomplete (origin: IR-004) |
| F-011 | HIGH | RESOLVED | Generic ToolProvider side-effect safety contract is incomplete (origin: IR-008) |
| F-012 | HIGH | RESOLVED | Generic inbound trust and confirmation-evidence contract is incomplete (origin: IR-005) |
| F-013 | MEDIUM | RESOLVED | Duplicate inbound redelivery can create ghost conversation state (origin: IR-005) |
| F-014 | MEDIUM | RESOLVED | PostgreSQL schema startup verification is hidden and mutating (origin: IR-006) |
| F-015 | MEDIUM | RESOLVED | Non-prefix migration history can execute migrations out of order (origin: migration-metadata integrity review after F-014) |

Contagem: RESOLVED 11 (F-001, F-004, F-006, F-008, F-009, F-010, F-011, F-012, F-013, F-014, F-015) · OPEN 0 · SUPERSEDED 2 (F-003, F-005) · PARTIALLY_RESOLVED 2 (F-002, F-007) · ACCEPTED_RISK 0.
Não há `BLOCKER`. O único `HIGH` não resolvido é F-002 (`PARTIALLY_RESOLVED` em 2026-10-17: evidência de sandbox registrada; falta rodar contra o gateway implantado com número real antes do piloto).

---

### F-001 — Baseline verificável do repositório
- **Severity:** MEDIUM
- **Status:** RESOLVED (2026-10-07, base `a9c7af9`)
- **Evidence:** `ruff check .` PASS; `ruff format --check .` PASS (360 arquivos); `mypy src examples` PASS (218 arquivos); `lint-imports` PASS (9 contratos); `pytest` PASS (1649 passed, 8 deselected, 0 skips, 414 s; inclui PostgreSQL real, chaos C01–C16, invariantes, arquitetura, evals); `app.ops check` PASS. Detalhe na tabela do baseline.
- **Consequence:** o código do repositório tem baseline explícito e reproduzível; aceitar a iteration não depende de evidência de produção.
- **Scope note:** F-001 cobre só o baseline do repositório. Itens externos ficam em seus próprios findings: RelayPlane real → F-002; restore/DR → F-003 e observabilidade operacional → F-005 (ambos reclassificados por ADR-022); CI do framework → F-007. Testes live com LLM são opt-in (`NOT_VERIFIED`) e `migrate check` de um deployment concreto é evidência de ambiente (consumidor); nenhum dos dois condiciona o baseline.
- **Expected resolution:** repetir a tabela (data + commit) a cada iteration que mude o código.

### F-002 — RelayPlane live contract evidence
- **Severity:** HIGH
- **Status:** PARTIALLY_RESOLVED (2026-10-17)
- **Evidence:** `docs/RELEASE_EVIDENCE.md` tem a tabela de registro em branco; `tests/contracts/test_relayplane_live.py` (3 testes) foi `skipped` nesta execução por falta de `RELAYPLANE_*`; `IMPLEMENTATION_STATUS.md` marca o achado 5 da auditoria externa e o 6 da Fase 14.1 como “pendente (você)”; `RELAYPLANE_CONTRACT.md` foi lido no commit `880820a` do repositório do gateway, sem registro de reconferência. Pista não verificada: há um container local `relayplane/evolution:patched-test`; não está registrado que o teste tenha rodado contra ele.
- **Consequence:** INV-021/034/061/066 e ADR-005/008 dependem de duas premissas do gateway (mesma `Idempotency-Key` dentro da retenção devolve a mesma mensagem; `/limits` reporta a retenção real). Sem evidência, um gateway que não deduplicasse geraria mensagens duplicadas, e o desenho assume o contrário.
- **Expected resolution:** executar o roteiro de `RELEASE_EVIDENCE.md` contra o gateway de produção (seções 1 e 2), preencher e versionar a tabela com commit e versão do gateway; repetir a cada mudança de versão/retenção do gateway.
- **Progress (2026-10-17):** the live contract test ran against a local RelayPlane sandbox (gateway 0.2.0 with the provider simulator): 2 passed, 1 skipped (retention measurement opt-in); `/limits` reported 86400 s; signed `message.received`/`message.outbound_status` deliveries were applied end to end by the POC. Recorded in `RELEASE_EVIDENCE.md` ("Recorded runs"). Found and fixed on the way: the live test could not reach a plain-HTTP sandbox (now opt-in `RELAYPLANE_ALLOW_INSECURE=1`) and `RelayPlaneSender.delivery_policy` hid the reason (now says TLS/connection/credential); `RELEASE_EVIDENCE.md` §2 named `message.status`/`provider_accepted_at` instead of `message.outbound_status`/`accepted_at`. Remaining for `RESOLVED`: the same run against the deployed gateway with a real number.

### F-003 — Disaster recovery rehearsal
- **Severity:** HIGH (original)
- **Status:** SUPERSEDED (2026-10-07) pela divisão de responsabilidades da ADR-022
- **Evidence (preservada):** `docs/OPERATIONS.md` ("Backup and disaster recovery") definia o procedimento e dizia "Rehearse this"; a busca por `pg_dump|pg_restore|pg_basebackup` em código, testes e docs não encontrava nada além dessa prosa; RPO/RTO sem valores; `verify_integrity` existe e é testado (`tests/postgres/test_ops.py`), mas não prova um restore.
- **Reclassification:** backup, storage, política de snapshots, RPO/RTO e ensaio de restore de um deployment pertencem ao consumidor (ADR-022); o framework não os possui e não deve ser mantido `HIGH` por não ensaiar infraestrutura que não é dele.
- **Restore contract check (lacuna?):** `OPERATIONS.md` já dizia, antes desta mudança, as condições pós-restore do framework: schema compatível (`migrate check`), `verify_integrity` sem violação, workers retomam do estado durável, e o impacto sobre idempotência/outbox (ponto de restore vs. `idempotency_retention_seconds`; escritas externas posteriores ao ponto não estão no ledger). Não há lacuna de conteúdo; a seção foi apenas reorganizada em *Framework contract* × *Operator responsibility*. Nenhum finding novo de restore foi criado.

### F-004 — Documentation authority drift
- **Severity:** MEDIUM
- **Status:** RESOLVED (2026-10-07)
- **Evidence (divergências encontradas, todas só documentais):**
  1. `ROADMAP.md` se declarava “Fonte única das fases” mas não contém as Fases 11, 12 nem 14.1/15a/15b, que existem em `IMPLEMENTATION_STATUS.md` e no código.
  2. Três regras de precedência incompatíveis: `IMPLEMENTATION_STATUS.md` (INVARIANTS > RUNTIME_PROTOCOL > DESIGN > ROADMAP), `DESIGN.md` §52 (INVARIANTS/RUNTIME_PROTOCOL/ROADMAP prevalecem sobre o DESIGN) e `docs/README.md`.
  3. `ROADMAP.md` (Fase 15): “nenhuma tool recebe a mídia” — falso desde a 15b (INV-068).
  4. `DESIGN.md` §1.4: “INV-001 a INV-022” — o conjunto vai até INV-071.
  5. `IMPLEMENTATION_STATUS.md`: cabeçalho “Fase 13 — Uso real (em andamento)” enquanto o ROADMAP e o corpo a dão por concluída; “Fase atual: 15”.
  6. `docs/README.md` não conhecia a hierarquia `00`–`06`.
  7. `RUNTIME_PROTOCOL.md` §8 dizia que o RelayPlane não define janela outbound formal; o contrato publicado (`RELAYPLANE_CONTRACT.md`) a expõe em `GET /limits`.
- **Resolution:** ADR-020 fixa a hierarquia; cada documento atual tem dono claro (`00`–`06`, `INVARIANTS`, `RUNTIME_PROTOCOL`); `ROADMAP.md`, `DESIGN.md` e `IMPLEMENTATION_STATUS.md` estão marcados como históricos; `README.md`, `docs/README.md` e `AGENTS.local.md` apontam para a memória canônica; as afirmações 1–7 foram corrigidas ou marcadas (nenhum texto histórico apagado; `RUNTIME_PROTOCOL.md` §8 foi reescrito para o contrato vigente).
- **Policy:** `DESIGN.md` permanece preservado como racional histórico e não é fonte do estado atual. Não há auditoria preventiva linha a linha; divergências futuras encontradas durante trabalho real geram findings específicos.

### F-005 — Operational observability gaps
- **Severity:** MEDIUM (original)
- **Status:** SUPERSEDED (2026-10-07) pela ADR-022, com resíduo do framework em F-009
- **Evidence (preservada):** (a) sem exportador OpenTelemetry (`core/tracing.py` é a interface; ADR-010); (b) métricas em memória por processo, sem agregação nem push; (c) orçamento de LLM avaliado com cache e sem rollup diário; (d) taxa de confirmações "não provadas" só em log; (e) o envio ao RelayPlane não propaga `trace_id` (o contrato do gateway não define); (f) `ops/alerts.rules.yml` e dashboard verificados só por consistência com `ops/slo.yaml`.
- **Reassessment:** a ausência de backend OTel, Prometheus central, Grafana ou agregação entre processos não é, por si só, defeito do framework; a pergunta é se ele oferece superfícies suficientes para o consumidor integrar. Superfícies verificadas no código: protocolos de métricas (`ports/metrics.py`: `ToolMetrics`, `LLMMetrics`, `RuntimeMetrics`) e renderização Prometheus; `Tracer` Protocol com `configure_tracer` (spans com `trace_id`/parent/duração, atributos redigidos); logs JSON estruturados com contexto fechado (`configure_logging`); `/healthz /readyz /metrics` (`ops_app`); `trace_id` persistido em inbox/outbox/ledger; extensão via ports/adapters. São suficientes para a responsabilidade do framework.
- **Residual:** (d) é lacuna de cobertura de métrica, não de superfície de integração, e segue como débito conhecido; (e) depende do gateway; (f) vira F-009 (artefatos first-party sem evidência de compatibilidade com o formato-alvo).

### F-006 — Entrypoint de produção com RelayPlane
- **Severity:** MEDIUM
- **Status:** RESOLVED (2026-10-07) por ADR-022
- **Evidence (preservada):** `RelayPlaneSender` e `webhook_app` são referenciados em `src/` somente nos próprios adapters; `app/serve.py` é o canal de terminal/desenvolvimento; o README instrui o consumidor a compor `Runtime.build(...)`. O finding já havia sido `SUPERSEDED` por uma decisão em aberto no roadmap.
- **Resolution:** a questão foi decidida: o framework é embeddable e não possui deployment runtime canônico (ADR-022). Não existir entrypoint oficial é comportamento arquitetural intencional; não há implementação pendente.

### F-007 — No CI pipeline in the repository
- **Severity:** MEDIUM
- **Status:** PARTIALLY_RESOLVED (2026-10-17)
- **Evidence:** `git ls-files` não contém `.github/`, `.gitlab-ci.yml` nem equivalente; a documentação cita “`app.ops check` roda no CI” (status Fase 10) e `ops check` foi desenhado para rodar sem asyncpg “no CI”.
- **Consequence:** os gates só existem como comandos manuais; nada prova que um push passa neles fora da máquina local, e a regra global de monitorar o CI do push não tem o que monitorar.
- **Scope:** CI **do próprio framework**. CI/CD da aplicação consumidora não é responsabilidade do projeto (ADR-022).
- **Expected resolution:** definir o provedor/pipeline e codificar os gates oficiais (incl. serviço PostgreSQL); confirmar a primeira execução verde.
- **Progress (2026-10-17):** `.github/workflows/ci.yml` codifies the official gates: job `gates` (Python 3.12 and 3.13: `uv sync --locked`, ruff check, ruff format --check, `mypy src examples`, `lint-imports`, `app.ops check`, pytest without PostgreSQL, `uv build`), job `postgres` (PostgreSQL 16 service: `pytest tests/postgres`, then `migrate apply` + `migrate check` on an empty database) and job `ops-artifacts` (F-009). Workflow checked with actionlint 1.7.7; every step reproduced locally against PostgreSQL 16: 1722 passed, 8 deselected. Remaining: the first green run on GitHub, which needs the current history pushed (GitHub `main` was at `aaed1d9` on 2026-10-17).

### F-008 — `mypy .` fora do escopo configurado
- **Severity:** LOW
- **Status:** RESOLVED (2026-10-07)
- **Evidence:** `mypy .` → 268 erros em 87 arquivos de `tests/` (imports `support.*`/`conftest`/`postgres.world` não resolvidos pela configuração etc.); `mypy src examples` passa. `[tool.mypy]` declara `src` e os exemplos, não `tests/`.
- **Resolution:** o gate oficial é `mypy src examples` (já documentado em `README.md` e `AGENTS.local.md`; explicitado em `06_AGENT_HANDOFF.md`). `mypy .` não é gate. Tipar `tests/` seria decisão/increment separado e não foi feito.

### F-009 — Shipped observability artifacts lack target-format compatibility evidence
- **Severity:** LOW
- **Status:** RESOLVED (2026-10-17)
- **Owner:** framework (first-party artifacts).
- **Evidence:** `ops/alerts.rules.yml` (Prometheus alert rules) and `ops/grafana-dashboard.json` (Grafana dashboard) are generated by `python -m conversation_agent.app.ops render` from `ops/slo.yaml` and are documented as such (`GUIDE.md` §8, `app/ops.py`); they are not labeled unsupported examples. The only checks are that the committed files equal what `render` produces (`app.ops check`, `tests/postgres/test_ops.py`); nothing reproducible shows they are valid for the formats/tools they claim (Prometheus rule-file schema and PromQL, Grafana dashboard JSON). Residual (f) of F-005.
- **Consequence:** the framework ships artifacts that claim a format and semantics it has not demonstrated. Does not block integration; a consumer can ignore them.
- **Expected resolution:** validate the artifacts in an automated or reproducible way against the relevant official format/tool, and record the evidence. Out of scope: creating or operating an observability backend, requiring a production Prometheus/Grafana, OpenTelemetry.
- **Resolution:** `ops/validate_artifacts.py` (standard library only) runs Prometheus's `promtool check rules` on `ops/alerts.rules.yml` and on every dashboard query (each turned into a recording rule, since promtool parses PromQL only in rule files), then imports `ops/grafana-dashboard.json` into a running Grafana through `POST /api/dashboards/db` and reads it back by uid, comparing the panels. CI job `ops-artifacts` runs it with promtool 2.55.1 (pinned sha256) and a `grafana/grafana-oss:11.3.0` service. Evidence 2026-10-17: alerts `SUCCESS: 21 rules found`; dashboard queries `SUCCESS: 21 rules found`; Grafana 11.3.0 `imported 'conversation-agent-health', 21 panels, schemaVersion 39`. A deliberately broken query makes the script fail. Still out of scope: an observability backend, OpenTelemetry.

### F-010 — Generic delivery safety contract is incomplete
- **Severity:** HIGH
- **Status:** RESOLVED (2026-10-08, base `566a114`, branch `feature/generic-delivery-contract`, commit `feat: enforce explicit delivery safety contract`)
- **Origin:** Integration Readiness IR-004.
- **Owner:** framework.
- **Evidence (before):** `Runtime.build(delivery_policy=None)` was valid and meant no policy: a blind-retry horizon of `outbox_retry_horizon=None` (unbounded) and no outbox reconciliation. RelayPlane's 24 h idempotency window was a default in generic code (`DEFAULT_UNKNOWN_BLOCKS_FOR` in the PostgreSQL outbox store, `unknown_blocks_for=timedelta(hours=24)` in `OutboxWorker` and the `OutboxStore` port). `retry_horizon <= retention` ignored the safety margin. A retryable `FAILED` whose retry time ran out was recorded `UNKNOWN`. A policy required a `lookup`, although a channel may settle by pushed status; `QUEUED` could exist without a handle. The generic docs spoke of `sender_retry_horizon` and RelayPlane.
- **Consequence:** a consumer could run a Runtime that retries without bound or never reconciles, a channel with a shorter retention than RelayPlane's was silently mis-sized, and a proven failure could be reported as ambiguity (an operator-facing false alarm that also blocks the conversation).
- **Resolution:** ADR-023; INV-034/065/066 reworded, INV-072/073 added. `DeliveryPolicy` is mandatory on `Runtime.build` and validated (`0 <= retry_horizon <= retention - safety_margin`; `retry_horizon == 0` is the explicit no-retry, used by the console channel). `outbox_retry_horizon` removed. The 24 h default is gone from the port, the store, the worker and the reconciler (the worker receives `safe_resend_window` from the policy; RelayPlane keeps its own via `RelayPlaneSender.delivery_policy`). `SendResult(QUEUED)` requires a handle. `MessageLookup` is `runtime_checkable`, optional and detected on the sender (`Runtime.build(lookup=...)`); the reconciler always runs and does not touch handle-bearing rows when there is no lookup (push settles them, monotonically). Proven `FAILED` and ambiguous stale `SENDING` stay distinct without a migration: `claim_ready` reports `claimed_from` (not stored) and `record_result` clears the reconciler's resend authorisation when a proven `FAILED` goes back to `PENDING`. Proven `FAILED` out of horizon/attempts ends `FAILED`; stale `SENDING` out of the horizon ends `UNKNOWN`; no resend leaves the safe resend window.
- **Verification (2026-10-08):** `ruff check .` PASS; `ruff format --check .` PASS (368 files); `mypy src examples` PASS (218 files); `lint-imports` PASS (9 contracts kept); `python -m conversation_agent.app.ops check` PASS; `pytest` PASS: 1682 passed, 8 deselected, 0 skipped (baseline before this change: 1649 passed, 8 deselected). New tests: `tests/contracts/test_delivery_policy.py`, `tests/postgres/test_delivery_contract.py`, plus `test_runtime_paths.py`, `test_runtime_serve.py`, `test_relayplane_sender.py`, `test_model_invariants.py`; invariant-to-test mapping in `INVARIANTS.md`. No migration added. Live RelayPlane evidence stays with F-002.
- **Review amend (2026-10-08):** `MessageSender` now carries the generic contract (docstring pointing to ADR-023 and `RUNTIME_PROTOCOL.md` §8); `retry_horizon` (ordinary/direct retries; 0 disables every resend) is documented apart from `safe_resend_window` (absolute envelope; the reconciler may authorise an unknown no-handle resend after the horizon); ADR-022 no longer uses `sender_retry_horizon`. No behavior change.
- **Compatibility (intentional breaking changes, no compatibility layer):** `Runtime.build` requires `delivery_policy`; `outbox_retry_horizon` removed; `DeliveryPolicy.reconcile_margin` → `safety_margin`; `safe_resend_until` → `safe_resend_window`; `OutboxWorker` built with `policy`/`coordination` (no `retry_horizon`, `unknown_blocks_for`); `OutboxStore.claim_ready` requires keyword `unknown_blocks_for`, `claim_unsettled` gains `with_handle`; `SendResult(QUEUED)` without handle is invalid. Upgrade: no first-party sender could have written a durable `QUEUED` row without `channel_message_id` (RelayPlane always records the gateway id; console never returns QUEUED; the fake sender only when a test scripts it), so only a custom sender could have; such a row is never claimed by the reconciler and stays visible as unsettled until an operator resolves it. No migration.
- **Residual:** a channel that can answer non-terminal must offer lookup, push or both; the framework cannot prove that at build time (needs capability metadata, out of scope). The attempt cap (`PostgresOutboxStore(max_attempts=5)`) is not exposed as configuration here. A stale handle-bearing `RECONCILING` row is not reclaimed by a lookup-less Runtime, but it remains push-settleable (`apply_channel_status` accepts `RECONCILING`; proven by `test_a_push_settles_a_row_left_reconciling_by_a_reconciler_that_disappeared`, monotonic afterwards) and a later Runtime with lookup may reclaim it after the claim expires. Not an F-010 correctness blocker.

### F-011 — Generic ToolProvider side-effect safety contract is incomplete
- **Severity:** HIGH
- **Status:** RESOLVED (2026-10-08, base `44f1fa8`, branch `feature/tool-provider-safety-contract`, commit `docs: define tool provider side-effect safety contract`)
- **Origin:** Integration Readiness IR-008.
- **Owner:** framework.
- **Evidence (before):** the `ToolProvider` port said only that providers do not raise and that an ambiguous write is `unknown`. The facts the runtime relies on lived in the HTTP/MCP adapters: `context.invocation_id` reaching the external system (`Idempotency-Key`, `adapters/tools/http.py`, `mcp.py`) and the write coercion to `unknown` (`tools/error_mapping.py`). `ToolRunner._safe_to_retry` trusts the provider's status and `retryable`; `_ledger_status` records any `technical_error`/`timeout` as final `FAILED`, never reconciled. A custom provider could mint a fresh dedupe identity per retry, or report an ambiguous write as a retryable `technical_error`/`timeout`, and duplicate a write or hide the ambiguity under a correctly configured agent. The first-party adapters were conformant (no misclassification found).
- **Consequence:** the public extension point could silently break INV-006/INV-021.
- **Resolution:** ADR-024; the `ports/tool_provider.py` docstring carries the contract (identity, external binding, classification, meaning of `retryable`, exceptions); `RUNTIME_PROTOCOL.md` §9 and a `GUIDE.md` custom-provider section; INV-006/INV-021 clarified (no new invariant). No runtime retry algorithm changed, no signature changed, no first-party provider changed; HTTP and MCP already followed the contract. Non-conformant custom providers must change their identity handling and classification (contract tightening).
- **Verification (2026-10-08):** new `tests/contracts/test_tool_provider_safety_contract.py` (same `context.invocation_id` across `ToolRunner` retries; ambiguous write -> `unknown` and no runner retry; business refusal final; write/read provider exception; HTTP retry with one `Idempotency-Key`/`X-Invocation-Id`; MCP session-recovery resend with one key). Existing evidence reused: `test_retry_and_risk_rules.py` (compiler rules and retry semantics), `test_http_tool_provider.py::test_write_timeout_is_unknown_never_a_safe_error`, `test_mcp_provider.py`, `test_reconciliation.py` (C04/C06 same key after recovery), `test_binding_and_runner.py::test_provider_raising_is_contained_and_conservative`.
- **Residual:** `idempotency_supported` is an external fact the framework cannot verify, and tools have no idempotency-retention field, so the guarantee must cover the reconciliation resend window (documented, not modelled). An operator `error_map` that marks a sub-500 status retryable on a write, and MCP's reading of a repeated session 404 as not executed, are operator/protocol assertions under the same contract.

### F-012 — Generic inbound trust and confirmation-evidence contract is incomplete
- **Severity:** HIGH
- **Status:** RESOLVED (2026-10-08, base `46a1785`, branch `feature/generic-inbound-contract`, commit `feat: enforce generic inbound channel contract`)
- **Origin:** Integration Readiness IR-005.
- **Owner:** framework.
- **Evidence (before):** (1) the contract a custom inbound adapter must meet was spread across the PostgreSQL inbox, the turn query, the confirmation logic and the RelayPlane mapping; `GUIDE.md` listed three fields. (2) A fresh `event_id` per delivery defeats dedupe and duplicates turns. (3) `conversation_id` is unique per TENANT and bound to one channel/contact/scope, which was undocumented (two channels sharing a string collide or share state). (4) Nothing said that `provider_occurred_at` must be provider-clock evidence comparable with `provider_accepted_at`: copying a local `received_at` into it can make a "yes" sent before the prompt look like a reply to it (INV-026 covered only the outbound side). (5) Ordering (`source_sequence` NULLS LAST, then `occurred_at`, `received_at`, `event_id`), the roles of the three timestamps, `session_id`/`kind`/`trace_id`/`scope` and the `receive` outcomes were undocumented. Also: `RUNTIME_PROTOCOL.md`/`DESIGN.md` called the 2 s confirmation tolerance configurable (it is fixed) and INV-035 said the channel comes from the subscription (RelayPlane takes the signed `instance_id`, checked against `instance_ids` when set). `event_id=""` was accepted.
- **Consequence:** a structurally valid custom adapter could silently break dedupe, conversation isolation, ordering or the confirmation proof of protected actions.
- **Resolution:** ADR-025 and the public contract: `InboundEvent`, `InboxStore.insert_if_absent` and `Runtime.receive` docstrings; `GUIDE.md` §5 (consumer journey and field table); `RUNTIME_PROTOCOL.md` §2 and §7. INV-020, INV-026 and INV-035 clarified; INV-074 (conversation identity) and INV-075 (inbound ordering) added. F-013 separately fixed the ghost-conversation bug and is unchanged; F-012 closes the public generic contract. The only behavior change is that an empty `event_id` fails at model construction. The confirmation algorithm and its 2 s tolerance, RelayPlane behavior, dedupe, ordering and the schema did not change; no migration.
- **Verification (2026-10-08):** new `tests/contracts/test_inbound_contract.py` (empty `event_id` rejected, ordinary and `system:` ids valid, naive timestamps rejected); `test_sequenced_events_sort_ahead_of_unsequenced_ones` (NULLS LAST); `test_a_local_receipt_time_after_the_prompt_is_not_confirmation_evidence` (prompt accepted with a provider time, reply with only a local receipt time a minute later: unproven, no write executed, re-prompted); `test_the_channel_is_the_signed_instance_checked_against_the_allow_list` (channel = signed instance, allow-list enforced, tenant from registration, trace id minted and persisted). Reused: F-013 and INV-020 tests, the identity/scope conflict tests, `test_confirmation_rules.py` (reply-to, provider timestamps, +-2 s ambiguity), `test_a_burst_is_ordered_and_aggregated_into_one_turn`, the late-event test. Gates: see the history entry.
- **Residual:** the framework cannot verify that a custom adapter honors the contract (no skew clamp, no sequence validation, `received_at` is caller-supplied); `session_id` keeps the first value per conversation (known debt, not redesigned).
- **Amend (2026-10-08, adversarial review, wording only, no behavior or test change):** narrowed (1) the unexpected-exception semantics of `Runtime.receive` (durable acceptance is unconfirmed, not "definitely not persisted"; the adapter redelivers the same event with the same `event_id`, safe through INV-020) and (2) the `source_sequence` rule (meaningful comparability among events that can be aggregated together, not a blanket "never resets"). Evidence retained from the increment: `pytest` 1702 passed, 8 deselected, 0 skips.

### F-013 — Duplicate inbound redelivery can create ghost conversation state
- **Severity:** MEDIUM
- **Status:** RESOLVED (2026-10-08, base `c149f0c`, branch `fix/inbound-dedupe-atomicity`, commit `fix: make inbound dedupe state-idempotent`)
- **Origin:** Integration Readiness IR-005.
- **Owner:** framework.
- **Evidence (before):** `PostgresInboxStore.insert_if_absent` ran `ensure_conversation` BEFORE the `INSERT ... ON CONFLICT (tenant_id, channel_id, event_id) DO NOTHING`. A redelivery of an existing `(tenant, channel, event_id)` carrying another `conversation_id` created that conversation's `conversation_states` row, found the event duplicate, returned `False` and committed: an empty ghost conversation. Reproduced before the fix by `test_a_duplicate_with_another_conversation_id_creates_no_conversation` and `test_concurrent_duplicates_for_two_conversations_leave_no_ghost` (both failed on `c149f0c`).
- **Consequence:** a duplicate was not a state no-op (INV-020 only promised "no duplicate turn"); a stray conversation (unowned scope/identity) could be created by a redelivery.
- **Resolution:** root cause was the write order. The event is now claimed first (the unique constraint is the arbiter); a duplicate returns `False` before any conversation write; for a new event `ensure_conversation` runs in the same transaction and an identity/scope conflict rolls the event back with it. No preflight `SELECT`, so no TOCTOU: concurrent contenders serialize on the unique index. First writer stays authoritative; no payload comparison, no new exception. `Runtime.receive` semantics (`True` new / `False` existing) unchanged. No migration (the schema has no FK between the tables). INV-020 clarified.
- **Verification (2026-10-08):** the two tests above (8 concurrent contenders over 2 conversation ids x 10 rounds: exactly one `True`, exactly one conversation, the winner's); existing `test_duplicate_event_never_creates_duplicate_turn` (8 concurrent same-conversation redeliveries), `test_a_conversation_id_cannot_be_reused_by_another_channel_or_contact` (new event, other channel/contact -> `ConversationIdentityConflictError`, event not stored) and `test_a_conversation_never_changes_scope` (now also asserts the refused event is not stored). Gates: see the history entry.
- **Residual:** a duplicate that disagrees with the stored event (other conversation, contact, text) is silently ignored by design (first writer wins); the generic inbound contract is documented separately (IR-005 documentation, not part of this finding).

### F-014 — PostgreSQL schema startup verification is hidden and mutating
- **Severity:** MEDIUM
- **Status:** RESOLVED (2026-10-08, base `4d61d68`, branch `feature/schema-startup-preflight`, commit `fix: make schema startup verification read-only`)
- **Origin:** Integration Readiness IR-006.
- **Owner:** framework.
- **Evidence (before):** `Migrator.ensure_current` called `status()`, which called `_bootstrap()`: `CREATE TABLE IF NOT EXISTS schema_migrations` and `ALTER TABLE ... ADD COLUMN IF NOT EXISTS checksum` on every call, so a "check", `app.migrate status` or a `/readyz` probe created metadata on a fresh database and required DDL privileges. Nothing on the startup path (`PostgresDatabase.connect`, `Runtime.build/receive/run/tick`, `app.serve`) called it; `GUIDE.md` only said "migrate first". `02_ARCHITECTURE`, INV-047, ADR-012, `OPERATIONS.md` and `IMPLEMENTATION_STATUS.md` said an ahead/edited schema "prevents startup / refuses to run", which was documentation overstatement: only `apply` rejected those states automatically. The supported PostgreSQL version was unstated (evidence: `postgres:16-alpine`, tests run on 16, no CI, no version check).
- **Consequence:** an embedding host had no clear, safe, supported way to prove schema compatibility before durable work, and the available guard mutated the database it was checking.
- **Resolution:** ADR-026. `Migrator.status()` and `ensure_current()` are read-only (they read the metadata only if `to_regclass` finds it; a never-migrated database is "all pending", hence `SchemaOutOfDateError`, and stays untouched; a table without the `checksum` column is read as NULL checksums); only `apply` bootstraps, locks, stamps and migrates (behavior unchanged). New canonical preflight `PostgresDatabase.ensure_schema_current()`; `app.migrate check/status`, `/readyz` and `verify_integrity` use the same check. `Runtime` intentionally stays I/O-free and does not auto-verify (no `build`/`receive`/`run`/`tick`/worker change, no verified-database type); the explicit host preflight is the supported contract. INV-047 clarified (apply refusal vs runtime precondition vs read-only verification); docs corrected; PostgreSQL 16 is the only tested version claimed; mixed-version rollout is documented as not a verified contract. No migration, no SQL change.
- **Verification (2026-10-08):** new tests in `tests/postgres/test_migrator.py`: `test_verifying_a_never_migrated_database_fails_and_creates_nothing` (verifier and `status` on a fresh DB: `SchemaOutOfDateError`, `schema_migrations` absent, relation count unchanged), `test_an_explicit_migration_is_what_makes_the_schema_verifiable`, `test_a_partly_migrated_database_is_behind_and_the_verifier_applies_nothing` (prefix applied, snapshot of rows/columns/relations identical after), `test_verifying_an_ahead_or_drifted_schema_fails_without_touching_it`, `test_status_and_verification_do_not_stamp_legacy_rows_or_add_the_column` (NULL checksums stay NULL; a table without the column is not altered), `test_verifiers_run_together_and_need_no_migration_lock` (8 concurrent verifiers while the advisory migration lock is held), `test_a_verifier_racing_an_explicit_migration_never_sees_a_half_made_schema`, `test_readiness_reports_the_real_schema_state_without_changing_it` (real behind/current/drift/ahead through `/readyz`, replacing reliance on the monkeypatched case), `test_integrity_on_a_never_migrated_database_reports_the_schema_and_creates_nothing`, `test_connecting_and_composing_stores_do_not_migrate_or_verify`; `test_cli_commands` extended (`check` and `status` on a fresh DB create nothing). Reused unchanged: apply, checksum, ahead, drift, concurrent-migrate and failed-migration tests. Gates: see the history entry.
- **Residual (not resolved by F-014; to be reviewed separately):** (1) non-prefix applied history: with 0001, 0002 and 0004 applied and 0003 missing, 0003 is classified "pending" and an explicit `apply` would run it after 0004; no gap detection exists; (2) legacy rows with a NULL checksum are treated as applied by the verifier and stamped by `apply` (trust on first use), so an edit to such a file is invisible until stamped. Both were re-confirmed by code inspection after this change; behavior unchanged. Also: the metadata table is named `schema_migrations` in the connection's default schema, a generic name that other migration tools use (not changed).

### F-015 — Non-prefix migration history can execute migrations out of order
- **Severity:** MEDIUM
- **Status:** RESOLVED (2026-10-08, base `1c5156b`, branch `fix/migration-history-gaps`, commit `fix: reject gaps in migration history`)
- **Origin:** migration-metadata integrity review after F-014 (the first residual recorded in F-014).
- **Owner:** framework.
- **Evidence (before):** `Migrator._status` classified each known migration independently (row present -> applied, absent -> pending). With 0009 missing and 0010 recorded, `apply` executed 0009 afterwards (executed in the architecture review with the shipped migrations): `published_agents` was dropped and recreated, `manifest_digest` (added by 0010) disappeared, registry data was lost and 0010 stayed recorded as applied.
- **Consequence:** metadata corruption was treated as an ordinary "behind" database and "repaired" destructively; `check` reported `SchemaOutOfDateError`, which tells automation to run `apply`.
- **Resolution:** ADR-027 / INV-076. Known recorded migrations must form a contiguous prefix. New status state `gap` (a known migration missing while a later known one is recorded; trailing missing ones stay `pending`; unknown rows create no gaps). New `MigrationHistoryError`, a direct sibling of the other migration errors. `status()` reports it without raising; `ensure_current`, `apply` and `apply --dry-run` fail closed, in `apply` before the NULL-checksum stamping and any migration. Precedence: drift -> ahead -> gap -> pending. No migration or schema change; NULL-checksum semantics unchanged; F-016 (metadata-table name collision) remains separate and unchanged. Additive public change: `MigrationStatus.state` may now be `gap`.
- **Verification (2026-10-08):** new tests in `tests/postgres/test_migrator.py`: `test_a_missing_shipped_migration_before_a_recorded_one_is_never_run_late`, `test_a_missing_first_migration_is_a_gap_and_later_absent_ones_stay_pending`, `test_a_normal_prefix_stays_pending_and_applies_forward`, `test_unknown_future_rows_alone_do_not_create_a_gap`, `test_ahead_wins_over_a_gap`, `test_drift_wins_over_a_gap_and_over_ahead`, `test_a_gap_is_refused_before_legacy_checksums_are_stamped`, `test_cli_refuses_a_history_gap`, `test_readiness_reports_a_history_gap` (the first uses the real 0009/0010: `gap`, all four entry points raise `MigrationHistoryError`, `published_agents` columns and row and the metadata snapshot unchanged; the last asserts `/readyz` 503 with `MigrationHistoryError`). Gates: see the history entry.
- **Residual:** legacy NULL-checksum trust on first use (unchanged, separate accepted debt); metadata-table naming (F-016).

---

## Histórico de resoluções

- 2026-10-07 (base `a9c7af9`): F-001, F-004, F-008 `RESOLVED`; F-006 `SUPERSEDED`. Evidência nas respectivas seções.
- 2026-10-07 (base `aaed1d9`, ADR-022): F-006 `RESOLVED`; F-003 e F-005 `SUPERSEDED`; F-009 aberto (LOW); F-002 e F-007 mantidos `OPEN`. Documentação apenas.
- 2026-10-08 (base `566a114`, ADR-023): F-010 aberto (HIGH, origem IR-004) e `RESOLVED` no mesmo increment, com a evidência acima. F-002, F-007 e F-009 inalterados.
- 2026-10-08 (base `44f1fa8`, ADR-024): F-011 aberto (HIGH, origem IR-008) e `RESOLVED` no mesmo increment, com a evidência acima. F-002, F-007 e F-009 inalterados.
- 2026-10-08 (base `c149f0c`): F-013 aberto (MEDIUM, origem IR-005) e `RESOLVED` no mesmo increment, sem migration. Gates: diff-check, ruff, format (369 arquivos), mypy (218), lint-imports (9 kept), `app.ops check` PASS; `pytest`: 1695 passed, 8 deselected, 0 skips. F-002, F-007 e F-009 inalterados.
- 2026-10-08 (base `46a1785`, ADR-025): F-012 aberto (HIGH, origem IR-005) e `RESOLVED` no mesmo increment, sem migration; F-013 inalterado. Gates: diff-check, ruff, format (370 arquivos), mypy (218), lint-imports (9 kept), `app.ops check` PASS; `pytest`: 1702 passed, 8 deselected, 0 skips. F-002, F-007 e F-009 inalterados.
- 2026-10-08 (base `4d61d68`, ADR-026): F-014 aberto (MEDIUM, origem IR-006) e `RESOLVED` no mesmo increment, sem migration. Gates: diff-check, ruff, format (370 arquivos), mypy (218), lint-imports (9 kept), `app.ops check` PASS; `pytest`: 1712 passed, 8 deselected, 0 skips. Resíduos (histórico não-prefixo; checksum NULL) registrados e inalterados. F-002, F-007 e F-009 inalterados.

- 2026-10-08 (base `1c5156b`, ADR-027): F-015 aberto (MEDIUM, origem revisão de integridade dos metadados de migration) e `RESOLVED` no mesmo increment, sem migration. Gates: diff-check, ruff, format (370 arquivos), mypy (218), lint-imports (9 kept), `app.ops check` PASS; `pytest`: 1721 passed, 8 deselected, 0 skips. NULL checksum e F-016 inalterados. F-002, F-007 e F-009 inalterados.

- 2026-10-17: F-009 `RESOLVED` (`ops/validate_artifacts.py` + CI job `ops-artifacts`); F-007 `PARTIALLY_RESOLVED` (`.github/workflows/ci.yml`, first GitHub run pending); F-002 `PARTIALLY_RESOLVED` (sandbox evidence from the POC integration run). Also: `examples/relayplane-app` (integration example, ADR-022 readiness item), `DeliveryUnknown` alert, `CHANGELOG.md` with the consumer migration notes.

Ao resolver um finding, registre: ID, data, commit e a evidência (comando + resultado).
