# IMPLEMENTATION_STATUS

Autoridade por escopo (os arquivos em `docs/` não têm o sufixo `_v4`):

- `INVARIANTS.md` — invariantes normativas e mapa INV → teste;
- `RUNTIME_PROTOCOL.md` — protocolo de execução (lease, epochs, journal, side effects, outbox);
- `ROADMAP.md` — fases, DoD, GO/NO-GO e chaos gates;
- `DESIGN.md` — visão consolidada e racional.

Em conflito real entre eles, vale a ordem INVARIANTS > RUNTIME_PROTOCOL > DESIGN > ROADMAP, e o ROADMAP nunca
relaxa uma invariante.

**Fase atual:** 8 — Primeiro Pack (implementada; aguardando revisão/merge). Fases 1–7 em `main`. Próxima: Fase 9.

## Phase 1

Status: **PASS** (DoD satisfeito) · GO/NO-GO: **GO** (abstrações + sonda live com Groq). Evals de qualidade mais amplos ficam como melhoria contínua.

DoD (`ROADMAP.md` Fase 1):

- [✓] conversa multi-turn — `test_s1_simple_scheduling_conversation_is_multi_turn`
- [✓] FakeLLM e provider real passam o mesmo contrato — `tests/contracts/test_llm_provider_contract.py`
      (Fake + `AnthropicLLM` com client do SDK stubado; ver ressalva abaixo)
- [✓] availability real via HTTP — `reference-scheduling-api` em socket real (uvicorn) nos testes e2e
- [✓] input/output/list mapping — `test_mapping.py`, `test_binding_and_runner.py`
- [✓] error mapping canônico — `test_error_mapping.py`, `test_http_tool_provider.py`
- [✓] journal in-memory — `InMemoryTurnJournal`
- [✓] replay de LLM journaled sem nova chamada — `test_journal_replay.py`
- [✓] engine sem SDK/provider concreto — `test_architecture_core_engine_do_not_import_adapters` + import-linter

Itens implementados:

- Modelos tipados (`core/`): contrato canônico de LLM (`LLMRequest/Message/ContentPart/ToolDefinition/ToolCall/
  StructuredOutput/Response/Usage/StopReason`), `ToolContext`, `ToolResult`/`ToolError` (taxonomia canônica),
  `CapabilityDefinition`, `ToolDefinition`, `CapabilityBinding` (+ `ErrorMap`), `AgentDefinition`, `ConversationState`,
  `JournalEntry`, hashing canônico (`args_hash`, `request_hash`).
- Ports: `LLMProvider`, `ToolProvider`, `TurnJournal`, `Clock`.
- `tools/`: linguagem de mapping restrita (select, enum, transform, const, default, `from_each`), classificação de
  erros HTTP, risco efetivo, construção de `CapabilityRequest` validado/canônico.
- `engine/`: `PolicyGate` (allowlist + proteção), `ToolRunner` (único que chama provider), `CapabilityPipeline`
  (Capability → Binding → PolicyGate → ToolRunner), `TurnEngine` (agent loop com limite de steps),
  `TurnJournalCursor` (replay fail-closed).
- Adapters: `FakeLLM`, `ReplayLLM` (cassette derivável do journal), `AnthropicLLM`, `FakeToolProvider`,
  `HTTPToolProvider`, `InMemoryTurnJournal`, `SystemClock`/`FixedClock`.
- `app/cli.py`: loop multi-turn genérico. `examples/vertical-slice`: agente de scheduling (todo o domínio).
- `examples/reference-scheduling-api`: sistema externo FastAPI (`GET /availability`).

Testes (238 passam, 5 live deselecionados; sem LLM real no CI):

| Área | Arquivo | # |
|---|---|---|
| architecture | `tests/architecture/test_boundaries.py` | 51 |
| contract LLMProvider | `tests/contracts/test_llm_provider_contract.py` | 15 |
| contract ToolProvider | `test_tool_provider_contract.py`, `test_http_tool_provider.py` | 19 |
| mapping / error mapping / binding | `test_mapping.py`, `test_error_mapping.py`, `test_binding_and_runner.py` | 41 |
| engine (INV-002/003/013, replay INV-014/017/018) | `tests/engine/*` | 20 |
| GO/NO-GO e2e | `tests/evals/test_phase1_go_nogo.py` | 9 |
| live (marker `integration`) | `tests/integration/test_live_llm.py`, `test_live_scenarios.py` | 5 (orçamento diário) |
| hardening 1.1 | `test_model_invariants.py`, `tests/engine/test_hardening.py`, `test_http_tool_provider.py` | 40+ |

Qualidade: `ruff check` + `ruff format --check` verdes; `mypy --strict src examples` verde; `lint-imports` 6/6 contratos.

## Phase 1.1 — hardening antes da Fase 2

Status: **PASS** (revisão externa do código da Fase 1; todos os pontos verificados e corrigidos, cada um com teste).

- [✓] P1 colisão de nome de tool: nomes de capability `dominio.acao` em lower_snake sem `__` → `name.replace(".", "__")`
      é injetivo (`a.b__c` e `a__b.c` agora são rejeitados na definição). `CapabilityDefinition.llm_name`.
- [✓] P1 invariantes nos modelos: `ToolResult`/`CapabilityResult` (success ⇒ sem error; demais ⇒ error e sem data),
      `CapabilityOutcome` (result XOR proposal), `Evaluation` (forma segue a decisão). Journal revalida no replay.
- [✓] P1 pré-I/O vs pós-I/O: provider/conexão ausente = `technical_error` (nada foi tentado); crash/mapeamento de saída
      pós-envio de write = `unknown`.
- [✓] P1 path param HTTP: só `[A-Za-z0-9._~-]`, sem `.`/`..`; `/ \ ? # %` rejeitados antes de qualquer request
      (o teste que congelava o comportamento antigo foi substituído).
- [✓] P2 defeito de configuração HTTP = `technical_error INVALID_TOOL_CONFIGURATION`; `validation_error` só para
      argumentos de negócio.
- [✓] P2 `stop_reason=MAX_TOKENS` não vira resposta final: `halted="llm_truncated"`, tool call truncada não executa, journalado.
- [✓] P2 `AnthropicLLM.aclose()`; `close_llm` fecha qualquer adapter com client.
- [✓] docs: precedência por escopo; resíduos do status antigo removidos.

Decisão de governança: a recomendação de autoridade por escopo foi adotada; a regra de conflito do pedido original
(INVARIANTS > RUNTIME_PROTOCOL > DESIGN > ROADMAP) permanece como desempate.

### INV → teste (Fase 1)

| INV | Teste |
|---|---|
| INV-001 | `test_architecture_core_engine_do_not_import_adapters`, `test_import_linter_contracts_pass` |
| INV-002 | `test_llm_cannot_set_trusted_context_ids_or_secrets`, `test_trusted_context_comes_only_from_the_runtime` |
| INV-003 (parcial) | `test_external_operation_always_crosses_policy_gate_and_tool_runner`, `test_only_tool_runner_executes_tool_providers` |
| INV-006 (base) | `test_write_timeout_is_unknown_never_a_safe_error`, `test_error_map_cannot_downgrade_*` |
| INV-008 | `test_runtime_does_not_own_business_state`, `test_framework_never_imports_examples_or_external_systems` |
| INV-013 | `test_tool_and_user_content_never_raise_instruction_authority` |
| INV-014 | `test_replay_reuses_llm_response_and_tool_result_after_a_crash` |
| INV-017 | `test_journal_request_hash_divergence_fails_closed`, `test_journal_step_type_divergence_fails_closed` |
| INV-018 | `test_replay_reuses_turn_reference_time` |
| INV-019/020, 004–005, 007, 009–012, 015–016, 021–022 | fases 2–4 (nada implementado antes da hora) |

### GO/NO-GO

| # | Cenário | Resultado |
|---|---|---|
| 1 | agendamento simples (multi-turn) | ✓ |
| 2 | consulta/escolha de slots reais via capability | ✓ (dados do LLM == dados da API, verificados independentemente) |
| 3 | indisponibilidade + alternativa sem inventar horário | ✓ |
| 4 | correção de data/hora acompanha o estado | ✓ (proposta anterior substituída, `args_hash` muda) |
| 5 | falha HTTP (5xx, API fora do ar) sem sucesso/slots inventados | ✓ |
| 6 | chega a `CapabilityRequest` válido de `scheduling.create` sem executar | ✓ (API só recebe `GET /availability`) |
| — | sonda de qualidade com LLM real (Groq `openai/gpt-oss-120b`) | ✓ 2026-10-03: contrato live + só oferece slots reais da API |

Nenhuma mudança de domínio foi necessária em core/engine para os cenários (`test_runtime_does_not_own_business_state`
garante que não há vocabulário de scheduling no framework).

### Decisões tomadas

1. **Draft de criação = `CapabilityRequest` em `ConversationState.proposals`** (um por capability protegida).
   Capability com risco ≠ `read` (ou confirmação exigida) recebe `REQUIRE_CONFIRMATION` do PolicyGate; na Fase 1
   isso apenas valida args (schema da Capability), canonicaliza, calcula `args_hash` e registra o rascunho.
   Não é `PendingAction`: nada é confirmado nem executado. Genérico, sem hack de scheduling no engine.
2. **Tool names para o LLM** = `capability.replace(".", "__")`; nomes desconhecidos viram `policy_denied`.
3. **Campos desconhecidos nos args do LLM são rejeitados** (não ignorados) → `validation_error` (INV-002).
4. **Estado entre turnos guarda só texto user/assistant + propostas**; tráfego de tool fica no journal do turno.
5. **Journal na Fase 1**: `INBOUND_AGGREGATED` (com `turn_reference_time`), `LLM_REQUEST` (antes da chamada,
   com `llm_request_id = hash(turn_id, step_index, request_hash)`), `LLM_RESPONSE`, `CAPABILITY_REQUEST`,
   `POLICY_DECISION`, `TOOL_RESULT`, `TURN_COMPLETED`. `tool_call_id` do LLM nunca entra em identidades.
   `logical_step_id = turn_id:step_index` e `invocation_id` derivado por hash (§9.1) já existem no `ToolContext`.
6. **`error_map` é reforçado na aplicação**: timeout/5xx de não-read vira `unknown` mesmo que o binding diga o contrário.
   Falha de conexão (request provadamente não enviada) é `technical_error` retryable, inclusive para write.
7. **Layering**: `core ← ports ← tools ← engine` (`tools/` fica entre ports e engine, conforme §45); adapters dependem
   de core/ports/tools; `app` é composition root.
8. **Mapping**: JSONPath-lite próprio (`$`, `.k`, `[n]`, `[*]`), transforms nomeadas (`minutes_to_hours`,
   `hours_to_minutes`, `datetime_to_utc`). Sem execução de código arbitrário.
9. **HTTPToolProvider (Fase 1)**: URL = `connection.base_url + path` relativo; paths absolutos/`//` rejeitados;
   path params com `quote(safe="")` e `.`/`..` rejeitados; sem redirects; headers `X-Tenant-Id/Conversation-Id/
   Trace-Id/Invocation-Id` vêm do `ToolContext`. Endurecimento completo (SSRF/DNS/secrets) é Fase 3.
10. **Provider real adicional: `OpenAICompatLLM` (Groq)** em `adapters/llm/openai_compat.py`, sobre `httpx`
    (sem SDK). Passa o mesmo contrato LLM (`openai-compat-stub`); structured output via function call forçada.
    Seleção por env (`LLM_PROVIDER`/`LLM_API_KEY`/`LLM_MODEL`/`LLM_BASE_URL`, fallback para chaves legadas) em
    `app/llm_factory.py`; suporta groq, openai, openai_compat (qualquer servidor compatível) e anthropic.
11. Python alvo `>=3.12` (testado em 3.13.1).

### Débitos conhecidos

- Proposta antiga não é invalidada por cancelamento/rejeição do usuário (só substituída por nova proposta da mesma
  capability). Invalidação real vem com `PendingAction` (Fase 3) / Flow (Fase 4).
- (resolvido na Fase 2) `POST /bookings` e o lookup por idempotency key agora existem na API de referência.
- `TOOL_RESULT` no journal é provisório: na Fase 2 o fato externo passa a viver no ToolInvocation ledger
  (`execution_epoch`); o journal referenciará a invocation.
- `INBOUND_AGGREGATED` por turno único: sem Inbox/burst/ordering (Fase 2).
- Resolução de datas relativas fica a cargo do LLM com o `turn_reference_time` no prompt; normalização determinística
  pt-BR é Fase 4 (DESIGN §22).
- `mypy` cobre `src` e `examples`; `tests/` não é type-checado (imports de `conftest`/`support`).
- Retry de leitura e `recovery` strategies (Fase 3) não implementados; `ToolDefinition` ainda sem `retry`/`idempotency`.
- Histórico sem sumarização (apenas janela `max_history_messages`).

### Questões encontradas

- **Live (Groq `openai/gpt-oss-120b`)**: `uv run --env-file .env pytest -m integration`. Passaram: contrato live, sonda "só oferece
  slots reais", **S3** (dia indisponível → alternativa sem slots inventados) e **S6** (escolha de slot → draft válido de
  `scheduling.create`, nenhuma escrita na API). **S4** (correção "na verdade quinta") existe mas ainda não foi executado
  com LLM real (orçamento diário). Orçamento local: `LIVE_LLM_DAILY_CALLS` (padrão 20), `.live_llm_usage.json`; 429 zera o dia.
  Anthropic segue validado só contra o contrato com client do SDK stubado.
- Nenhuma contradição normativa entre os documentos foi encontrada; as lacunas (representação do "draft" na Fase 1,
  posição de `tools/` nas camadas) foram resolvidas como decisões locais acima.

## Phase 2 — Reliability core

Status: **PASS** após a Fase 2.1 de hardening (revisão externa; ver seção abaixo). DoD verificado contra PostgreSQL 16 real e a API de referência real; 357 testes.

DoD (`ROADMAP.md` Fase 2):

- [✓] duplicate inbound não cria turn duplicado — `test_duplicate_event_never_creates_duplicate_turn` (8 entregas concorrentes, 1 vencedor; UNIQUE no Postgres)
- [✓] claim de eventos só depois do conversation lease — `test_claim_happens_only_after_the_lease_and_the_loser_claims_nothing`
- [✓] outbox/scheduler sobrevivem restart — `test_runtime_survives_restart_open_turn_and_pending_outbox`, `test_timers_survive_a_restart_and_an_abandoned_claim_is_reclaimed`
- [✓] worker zumbi não altera conversation state — `C09` (UoW e coordinator)
- [✓] executor válido finaliza o ledger após perder o conversation lease — `test_lost_conversation_lease_can_finalize_ledger_only`, `C14`
- [✓] heartbeat impede expiração silenciosa; perda o torna stale — `test_heartbeat_prevents_silent_expiry_*`, `test_lost_heartbeat_makes_the_worker_stale_*`
- [✓] journal evita repetir LLM step persistido (`C10`), diverge fail-closed (`C15`), `turn_reference_time` estável através de restart
- [✓] `UNKNOWN` entra em reconciliation — `ReconciliationWorker` (`C05`, `C06`, `C11`, recovery do `C04`)
- [✓] nenhum I/O externo com transaction da conversa aberta — `test_no_database_transaction_is_open_during_llm_or_tool_io`
- [✓] chaos C04–C11 e C14–C16 — um teste nomeado por caso (`tests/architecture/test_chaos_gates.py` impede regressão da cobertura)

Implementado: schema v1 + migration 0002 (`adapters/postgres/migrations`); `PostgresLeaseStore` (epoch monotônico);
`PostgresUnitOfWorkFactory` (fence por `FOR SHARE` condicionado a (owner, epoch): takeover e commit de zumbi nunca se
intercalam); journal durável; `PostgresInboxStore`/`PostgresOutboxStore`/`PostgresScheduler`/`PostgresToolInvocationStore`;
`TurnCoordinator` (algoritmo §39D), `LeaseHandle` (heartbeat), `OutboxWorker`, `SchedulerWorker`, `LedgerToolExecutor`
(A/B/C1/C2), `ReconciliationWorker`; `ChaosFaults`/`SimulatedCrash` (BaseException) para fault injection; API de referência
com `POST /bookings` idempotente, lookup por chave e `DELETE`.

### Decisões de arquitetura da Fase 2 (afetam persistência/idempotência/fencing/retry/delivery)

1. **Fencing atômico pelo lock da linha da conversa.** Toda UoW começa com `SELECT ... FOR SHARE` condicionado a (owner,
   epoch); o takeover (`UPDATE ... epoch+1`) exige lock exclusivo e portanto espera UoWs em voo (teste dedicado).
2. **PREPARE e C2 atômicos com o journal** via `TurnJournalCursor.step_atomic` (invocation + `TOOL_PREPARED`; mark-applied +
   `TOOL_RESULT`, cada par em uma transaction). `invocation_id` determinístico (hash de tenant/conversa/turn/logical_step/
   args_hash) torna PREPARE idempotente no replay.
3. **Idempotency key = `invocation_id`**, estável em retry técnico, replay e reconciliation; enviada em `Idempotency-Key`.
4. **`UNKNOWN` não é aplicado à conversa**: fica `result_application_status=none` até reconciliar (atualizado em
   `RUNTIME_PROTOCOL.md`). O turno permanece aberto e é retomado do journal.
5. **Reconciliation**: claim com novo `execution_epoch`; `status_lookup` → achou = `RECONCILED`; "não existe" + tool idempotente =
   reenvio com a mesma key; sem contrato/não idempotente/esgotado = `HUMAN_HANDOFF`. Backoff por timer durável (`reconcile:<id>`).
6. **Tempo vem do `Clock` injetado** (lease, claims, backoff), nunca do relógio do banco — determinismo nos testes.
7. **Turno**: um por vez por conversa (índice único de turn aberto); burst = todos os eventos READY no claim, ordenados por
   `source_sequence`/`occurred_at`/`received_at`/`event_id`; evento tardio (≤ watermark) entra no próximo turno, marcado.
8. **Ownership ≠ BOT** (HUMAN e HANDOFF_PENDING, por ora) persiste contexto e não gera LLM/tool/outbound (INV-019).
9. `PolicyGate` continua segurando writes (Fase 3). Os testes de chaos usam `AllowWrites` (apenas teste) para exercitar o
   protocolo de ponta a ponta com escrita real na API de referência.

### Débitos conhecidos (Fase 2)

- Sem debounce/janela de silêncio no burst (todo READY entra no turno); `confirmation`/`PendingAction` são Fase 3.
- Outbox `UNKNOWN` ainda não é reconciliada (depende do MessageSender real, Fase 6) e não há `SUPERSEDED`/re-prompt (Fase 3).
- Falha permanente de LLM só marca o turno `FAILED` após `max_turn_attempts`; não há mensagem de desculpas automática.
- Workers são `run_once()` chamáveis; o loop residente/processo (`app/worker.py`) e OpenTelemetry básico ficam para o hardening.
- Cancelamento cooperativo (`C12`) é Fase 4. HANDOFF_PENDING terá política própria.
- `tests/postgres` exige o container (`docker compose up -d`); sem ele os testes são pulados com mensagem explícita.

## Phase 2.1 — hardening pós-revisão da Fase 2

Status: **PASS** (todos os achados verificados contra o código; os 3 P0 corrigidos antes da Fase 3). 357 testes.

| Achado | Resolução | Teste |
|---|---|---|
| P0 intent não congelada | `ExecutionIntent` (args mapeados, provider, connection, fingerprint, recovery) gravada no PREPARE; B/retry/reconciliation executam a intent; fingerprint divergente → `INTENT_CHANGED` (INV-023) | `test_prepared_invocation_executes_the_frozen_intent_*`, `test_a_changed_input_mapping_*`, `test_recovery_follows_the_frozen_contract_*`, `test_run_frozen_sends_exactly_*`, `test_a_deploy_between_prepare_and_execute_*` |
| P0 espera de reconciliation consumia `max_turn_attempts` | `ToolResultPendingError` → status `waiting`, sem contar; só falha real (LLM) incrementa `turns.attempts` (INV-025) | `test_pending_tool_result_never_exhausts_turn_attempts`, `test_llm_failures_still_exhaust_attempts_*` |
| P0/P1 lease expirado ainda abria UoW | UoW exige `lease_expires_at > agora`; heartbeat não ressuscita lease expirado (INV-024) | `test_an_expired_lease_cannot_open_a_unit_of_work_*`, `test_a_heartbeat_cannot_resurrect_*` (o teste que formalizava o comportamento antigo foi reescrito) |
| Relógio de coordenação | `CoordinationTime`: `clock_timestamp()` do Postgres por padrão; `Clock` fixo só nos testes | `test_coordination_time_defaults_to_the_database_clock_*` |
| P1 `provider_metadata` perdido | `CapabilityResult.provider_metadata` (interno; nunca renderizado ao LLM) chega ao ledger | `test_provider_metadata_is_recorded_in_the_ledger_but_never_shown_to_the_llm` |
| P1 qualquer `business_error` = "não existe" | `RecoverySpec.absent_codes`; só esses códigos autorizam reenvio | `test_only_declared_absent_codes_authorise_a_resend` |
| P1 reconciler single-agent | claim escopado por `agent_id`; versão fica protegida pelo fingerprint | `test_reconciliation_only_claims_invocations_of_the_agent_it_serves` |
| P2 read abandonado → handoff | default `safe_retry` para reads sem contrato | `test_an_abandoned_read_is_safely_re_run_*` |
| P2 `204 No Content` | sucesso conhecido (`data={}`), não `unknown` | `test_204_no_content_is_a_known_success_*` |
| P2 identidade `conversation_id` | contrato explícito: único no tenant e preso a um channel+contact; reuso é recusado | `test_a_conversation_id_cannot_be_reused_*` |
| Fase 3 (side effect + LLM morto) | turno que falha depois de um side effect emite aviso determinístico no outbox | `test_a_turn_that_fails_after_a_side_effect_still_notifies_the_contact` |
| Docs | INV-023/024/025, protocolo §4A/§Fase 2.1, status | — |

Débitos remanescentes: `AgentRegistry`/versão histórica por invocation (hoje o fingerprint falha fechado em vez de resolver a
versão antiga); uma `session_id` por conversa (o modelo conceitual admite várias); `blocked_on_invocation_id`/wake explícito
em vez de polling do turno em espera; autoridade de tempo do `LeaseHandle` local ainda usa o `Clock` da aplicação.


## Phase 3 — Protected actions, confirmation e security

Status: **PASS**. DoD verificado contra PostgreSQL 16 real e a API de referência real; 527 testes (após a 3.1).

DoD (`ROADMAP.md` Fase 3):

- [✓] protected action nunca executa sem prompt elegível + confirmação válida — `test_a_yes_before_the_prompt_was_accepted_never_confirms`, `test_protected_action_has_persistent_action_id_and_scoped_confirmation`
- [✓] "sim" anterior ao prompt ou ambíguo não confirma — `test_a_yes_that_predates_the_prompt_*`, `test_inside_the_skew_window_*`, `test_C13_confirm_while_prompt_ambiguous` (SENDING/QUEUED/UNKNOWN)
- [✓] "sim, mas..." invalida a ação anterior — `test_changed_protected_args_invalidate_confirmation` (INV-010)
- [✓] CONFIRMED nunca existe sem PREPARED após commit — `C02`, `C03`, `test_confirmed_never_exists_without_a_prepared_invocation` (INV-005)
- [✓] FAILED terminal exige nova tentativa semântica (novo `action_id`) — `test_a_failed_attempt_needs_a_new_action_never_a_retry_of_the_same` (INV-015)
- [✓] timeout/5xx de write ambíguo vira UNKNOWN, nunca retry cego — `test_ambiguous_write_in_a_confirmed_action_becomes_unknown_then_reconciles`, `test_unknown_is_never_retried_*`
- [✓] retry de write sem idempotência é rejeitado pelo compiler — `ToolDefinition` (`RetryPolicy`) + `test_a_write_cannot_retry_without_idempotency_support` (INV-021)
- [✓] chaos **C01, C02, C03, C13** passam (meta-teste exige os 15 casos das Fases 2 e 3)

Implementado: `PendingAction`/`ActionConfirmation` (migration 0005, `pending_actions` com UNIQUE `(tenant, action_id)` e no
máximo uma ação aguardando por conversa); evidência de canal no inbox (`reply_to_provider_message_id`, `provider_occurred_at`);
interpretação por regras + fallback LLM validado; `assess_eligibility` (reply-to → domínio de tempo do canal + skew →
ambíguo); `ConfirmationStage` (confirm+prepare atômico, reject, modify, re-prompt limitado, expiração, execução pelo ledger,
composição com fallback determinístico); `PolicyGate` com regras compostas (allowlist por tenant que só estreita, ownership,
orçamento de chamadas, loop, limite numérico, janela de horário); guardrails (orçamento de tokens, truncamento de resultado,
redaction); `RetryPolicy` + regras de compilação + retry seguro no `ToolRunner`; `ConnectionResolver`/`SecretProvider` e
`HTTPToolProvider` hardened (HTTPS, allowlist de host, SSRF/redes privadas, pin de IP contra DNS rebinding, sem redirects,
limites de request/response, timeout só pode ser reduzido, credencial injetada na chamada e nunca devolvida).

### Decisões de arquitetura da Fase 3 (persistência/confirmação/retry/delivery)

1. **PendingAction nasce na mesma transação do prompt** (outbox com `action_id`); `action_id` determinístico por turno.
2. **A pergunta de confirmação é do runtime** (`ConfirmationTexts`), anexada à resposta do modelo; o modelo é instruído a não pedir.
3. **Decisão + elegibilidade são journaladas** (`CONFIRMATION_DECISION`) e o `action_id` vai no payload: um turno retomado
   volta ao mesmo estágio mesmo com a ação já `CONFIRMED` (bug real encontrado pelo `C03`).
4. **Só `confirm` exige prompt elegível**; `reject`/`modify` não precisam de prova. `BEFORE_PROMPT`/`UNRELATED_REPLY`
   seguem como mensagem normal; `AMBIGUOUS`/`PROMPT_NOT_ACCEPTED`/`unclear` re-perguntam (novo `outbox_id`, UNKNOWN → SUPERSEDED).
5. **Confirmação por LLM nunca é suficiente sozinha**: confiança mínima (0,85), saída malformada = `unclear`, e ainda
   exige prompt elegível e `args_hash` inalterado dentro da transação.
6. **`invocation_id` de ação protegida = `hash(tenant, conversa, action_id)`** (§9.1), independente do turno.
7. **Depois do side effect o turno nunca falha por composição** (fallback determinístico).
8. **Retry**: write só com idempotência + mesma chave, e só para falha que provadamente não chegou ao sistema; `unknown` nunca.
9. **HTTP**: o IP validado é o IP usado (Host/SNI preservam o nome); qualquer resposta DNS mista/privada rejeita o destino;
   `send(..., follow_redirects=False)` é forçado mesmo com client injetado (bug encontrado em teste).
10. `PolicyGate.evaluate` aceita `PolicyContext` (tenant, ownership, contadores do turno); `refine` roda regras que precisam do
    request validado e um DENY ali impede até a proposta.

### Débitos conhecidos (Fase 3)

- Rate limit por contato/tenant/conexão e circuit breakers/backpressure por provider (guardrails operacionais restantes) —
  Fases 7/10. Expiração de `PendingAction` é verificada no uso (sem timer dedicado no scheduler ainda).
- Reconciliação de prompt `UNKNOWN` por consulta ao canal (hoje: re-prompt + SUPERSEDED) depende do MessageSender real (Fase 6).
- `confirmation_clock_skew_tolerance` é parâmetro do estágio (2 s); falta configuração por canal.
- Uma única `PendingAction` aguardando por conversa (índice único); múltiplas ações simultâneas ficam para depois.
- O resumo de confirmação mostra os argumentos como o modelo os deu (ISO com offset); formatação amigável por capability fica
  para os Flows (Fase 4).


## Phase 3.1 — hardening pós-revisão da Fase 3

Status: **PASS** (achados verificados contra o código antes de corrigir). Novas invariantes **INV-026** e **INV-027**. 527 testes.

| Achado | Resolução | Teste |
|---|---|---|
| P0 `provider_accepted_at` era o relógio local | `SendResult.provider_accepted_at` (canal ou `None`); o outbox nunca carimba horário local; sem prova comparável → ambíguo → pergunta de novo (INV-026) | `test_a_channel_that_gives_no_timestamp_*`, `test_accepted_at_is_the_channel_time_*`, `test_a_legitimate_reply_is_eligible_when_the_channel_clock_runs_behind` |
| P0 intent congelava o *nome* da connection | `connection_fingerprint` (destino resolvido, sem segredos) no PREPARE; o provider confere antes de enviar; `DESTINATION_CHANGED` falha fechado (INV-027) | `test_prepare_freezes_the_destination_*`, `test_the_connection_fingerprint_covers_the_destination_but_never_secrets`, `test_a_prepared_write_is_never_sent_to_a_destination_*`, `test_the_execution_path_refuses_a_stale_destination_*` |
| P0 status lookup re-resolvido na reconciliation | `RecoverySnapshot.lookup_intent` congelado (args + destino); mudança → `LOOKUP_CHANGED`/`LOOKUP_DESTINATION_CHANGED` → handoff | `test_a_changed_lookup_binding_refuses_to_decide_absence` |
| P1 resultado do lookup assumido igual ao original | `RecoverySpec.result_map`; validação ao construir o agente (schemas idênticos ou `result_map`); lookup deve ser read, ligado e receber `idempotency_key` | `test_a_lookup_with_a_different_schema_is_converted_*`, `test_a_lookup_result_that_cannot_become_*`, `test_a_status_lookup_must_exist_*` |
| P1 catálogo do LLM sem filtro de tenant | `exposed_tools(PolicyContext)`; capability negada ao tenant não aparece | `test_a_capability_the_tenant_cannot_call_is_not_even_shown_*` |
| P2 retry não olhava `effective_risk` | `AgentDefinition` valida retry contra o risco efetivo do binding | `test_retry_rules_follow_the_effective_risk_not_the_tool_label` |
| P2 `SendResult` aceitava estados internos | validator: só QUEUED/ACCEPTED/FAILED/UNKNOWN | `test_a_sender_cannot_report_runtime_internal_outbox_states` |
| Docs | topo do status, INV-026/027, protocolo | — |

Decisão mantida e explícita: só `confirm` exige prova de prompt elegível; `reject`/`modify` não (cancelar demais é
preferível a executar demais).

Débitos remanescentes: a saída do lookup é lida pelo `output_map` *atual* do binding do lookup (o fingerprint congela
a entrada e o destino, não a interpretação); recovery de writes sem `status_lookup` continua dependendo só de
`retry_same_key`/handoff.


## Phase 4 — Flows e entendimento

Status: **PASS**. DoD verificado contra PostgreSQL 16 e a API de referência reais; 648 testes (+5 live deselecionados).

DoD (`ROADMAP.md` Fase 4):

- [✓] Flow corrige slot sem perder contexto válido — `test_correcting_the_time_keeps_context_and_does_not_search_again`, `test_correcting_the_day_searches_again_and_keeps_the_rest`, `test_correcting_the_service_discards_the_old_choice`, `test_changing_the_request_instead_of_answering_corrects_the_flow` (Postgres, com PendingAction invalidada)
- [✓] digressão permitida retorna ao Flow — `test_digression_is_answered_then_the_flow_resumes`; novo flow suspende/retoma o ativo — `test_a_different_request_suspends_the_flow_and_resumes_it`
- [✓] `cancel_requested` não interrompe external call em voo — `test_C12_cancel_during_external_call_finishes_call_then_stops_at_boundary`
- [✓] HUMAN nunca roda Router/LLM automaticamente — `test_human_owned_conversation_never_runs_flow_router_or_llm` (0 chamadas LLM, 0 requests, 0 outbound, nenhum flow criado)
- [✓] todo outcome canônico tem transição ou default seguro — `default` é obrigatório no modelo; `test_every_tool_outcome_has_a_transition_or_safe_default` (validation_error, business_error, technical_error, timeout, unknown), `test_policy_denied_follows_its_transition`, `test_a_conflict_at_execution_follows_the_flow_transition` (409 depois do "sim")
- [✓] normalização pt-BR determinística de data/hora/duração — `tests/engine/test_temporal_ptbr.py` (80 casos)
- [✓] chaos **C12** passa (meta-teste exige C01–C16 das Fases 2–4)

Implementado: `core/definitions/flow.py` (Flow tipado em Python: slots, `Collect`/`Invoke`/`Choose`/`Propose`, transições
`Say`/`Ask`/`Handoff`, validação na construção e no `AgentDefinition`), `core/models/flow.py` (`FlowInstance`/`StepResult`
dentro de `ConversationState.flows`, só dado conversacional), `core/temporal_ptbr.py`, `engine/flow_understanding.py` (extração
determinística, correção, escolha entre opções reais), `engine/flow_runner.py` (progresso derivado do estado, pilha de flows),
`TurnEngine.run_capability` (**único** caminho a uma capability, usado pelo agent loop e pelos Flows), `FlowIO` (extração
estruturada e digressão journaladas), cancelamento cooperativo (migration 0006, `Lease.cancel_requested`, `PostgresInboxStore(restart_on_new_message=)`,
`TurnRepository.cancel`, `TurnCancelledError`), flow de agendamento no exemplo (`SCHEDULING_FLOW`, opt-in `build_agent(flows=True)`).

### Decisões de arquitetura da Fase 4

1. **Progresso derivado, não cursor.** O runner percorre os steps e age no primeiro insatisfeito; `Invoke` só é satisfeito se
   o `input_hash` guardado bate com as entradas atuais. Corrigir um slot muda as entradas → o resultado deixa de valer sozinho
   e o que ainda vale (serviço, mesma busca) é reaproveitado (a correção de horário não repete a busca).
2. **Slots derivados caem na correção** (`SlotDefinition.invalidates`): mudar serviço/dia/horário esquece o horário escolhido.
3. **Opções vêm só da tool**: `Choose` lê a lista de um `Invoke` anterior; a escolha por número/ordinal/hora/dia só aceita o que foi
   mostrado (ordinais femininos "segunda/quarta/quinta" não são ordinais: são dias da semana).
4. **Entendimento em duas camadas**: regras primeiro (zero LLM); só se nada for entendido há UMA chamada estruturada journalada.
   O modelo só *aponta palavras* ("depois de amanhã"); o valor é calculado pelos parsers determinísticos a partir da data de
   referência do turno (INV-018) — um valor que o parser rejeita nunca entra no estado.
5. **Digressão = agent loop restrito a leitura** (`digression_capabilities`, validadas como read no `AgentDefinition`) mais a
   pergunta pendente reposta pelo runtime; limitada por `max_digressions`; qualquer outra tool pedida é negada.
6. **Respostas do Flow são texto determinístico** (templates da definição); LLM só entende/digressiona.
7. **Propose reutiliza a Fase 3 inteira**: o Flow só *propõe*; PendingAction, confirmação, ledger e execução são os de sempre.
   `StageResult.closed/status` fecha o Flow quando a ação executa/é rejeitada/expira; falha na execução segue a transição do `Propose`.
8. **Cancelamento só em safe boundary** (INV-012): `restart` marca `cancel_requested` quando chega mensagem com turno em
   andamento (default `queue` não marca); o worker lê no heartbeat e checa **antes de PREPARE**, nunca dentro de
   PREPARED→EXECUTING→chamada→finalize (o `guard` dos executors continua só de staleness). Sem efeito irreversível no turno → turno
   `CANCELLED` e eventos voltam a `READY` (a próxima rodada agrega com a mensagem nova; `turn_id` ganha sal se os mesmos eventos
   reabrem). Com efeito já feito → o turno termina com aviso determinístico (`cancelled_after_effect_reply`): o efeito real é
   reportado, nunca descartado.
9. **Cancelamento por texto** ("deixa pra lá") só vale em mensagens curtas (≤ 6 palavras) e tem precedência sobre trigger.
10. Um trigger de outro flow só suspende o ativo se a mensagem **não** responde o que o flow ativo pergunta (resposta ganha).

### Débitos conhecidos (Fase 4)

- Roteamento de intenção por LLM (escolher o flow sem trigger): hoje triggers por palavra/frase; sem flow nem trigger, cai no agent
  loop da Fase 1 (que ainda enxerga todas as capabilities, inclusive a protegida via proposta).
- `Handoff` só encerra o flow e avisa; a mudança de `Ownership` para HUMAN é do canal/Fase 6.
- `logical_step_id` de leituras do Flow é por turno/step; o `flow_instance_id + step_id + attempt_semantic_id` de DESIGN §20.3 só
  importa para escritas sem confirmação, que o Flow não faz (escrita sempre passa por `Propose`).
- Trigger "agendamento" também casa "cancelar meu agendamento" (não há flow de cancelamento ainda).
- `restart` é política do `PostgresInboxStore`; configuração por agente/canal fica para a Fase 5/6.
- Gatilhos e textos são pt-BR no exemplo; a normalização temporal é só pt-BR (outros idiomas = outro módulo).


## Phase 4.1 — hardening pós-revisão da Fase 4

Status: **PASS** (achados verificados no código antes de corrigir; todos reproduzidos). 663 testes.

| Achado | Resolução | Teste |
|---|---|---|
| P0 Flow/digressão validavam `cap.risk`, não o risco efetivo | `ResolvedToolBinding.requires_protection` é a regra única (policy, flows e recovery usam a mesma); `Invoke`/digressão exigem leitura *efetiva*, `Propose` exige proteção efetiva | `test_invoke_rejects_a_capability_that_is_only_a_read_by_label`, `test_digression_rejects_*` |
| P0 `safe_retry` sobrevivia a binding efetivamente write; lookup só era "read" por rótulo | `_check_safe_retry_recovery` (só leitura efetiva) e lookup precisa ser leitura efetiva em todas as camadas | `test_safe_retry_is_rejected_for_an_effective_write`, `test_a_status_lookup_must_be_an_effective_read_*` |
| P1 `Propose` não-terminal/múltiplo, mas o runtime encerra o Flow nele | v1 formalizada: `Propose` é o único e último step (validação na construção) | `test_propose_must_be_the_single_last_step` |
| P1 trigger dependia da ordem dos flows | maior `priority`, depois trigger mais específico (mais palavras); empate verdadeiro não inicia nada (agent loop); trigger idêntico em dois flows é erro de definição | `test_the_most_specific_trigger_wins_*`, `test_priority_breaks_ties_*`, `test_the_same_trigger_in_two_flows_*` |
| P1 pilha de flows ilimitada | `max_flow_depth` (default 3); flow já suspenso é trazido à frente (nunca duplicado) | `test_alternating_between_two_flows_never_duplicates_instances`, `test_depth_limit_*` |
| P1 `24:30`/`24h` viravam 00:xx do mesmo dia | hora 24 é recusada (o fluxo pergunta) | `test_hour_24_is_refused_*` |
| P2 erros de definição só em produção | validação: `Table` cobre todas as choices do enum, `AddDays` exige slot date, `max_options >= 1`, campos do `Choose` existem no output da capability de origem, timezone válida | `test_a_table_must_cover_*`, `test_add_days_*`, `test_choose_fields_*`, `test_an_unknown_timezone_*` |
| Runner | `Invoke` que recebe proposta/resultado nulo não derruba mais (`assert`): vira `unknown` e segue a transição | — |

Fica para a Fase 5 (explícito): `agent_version` participar do dispatch/reconciliation (`AgentRegistry` + `CompiledAgent`
versionado e pinado por conversa/flow); hoje um deploy entre PREPARE e a reconciliation falha fechado (handoff), não continua
na versão antiga. Os checks semânticos acima viram DoD do Compiler.


## Phase 5 — AgentDefinition + Compiler

Status: **PASS**. 725 testes (+5 live deselecionados), PostgreSQL 16 e API de referência reais. Novas invariantes **INV-028** e **INV-029**.

DoD (`ROADMAP.md` Fase 5): o compiler detecta, antes do runtime —

- [✓] referências ausentes (capability/tool/binding/lookup/flow/allowed, duplicatas) — `test_missing_references_are_all_reported_in_one_pass`, `test_an_allowed_capability_without_a_binding_*`, `test_a_status_lookup_must_point_to_*`, `test_duplicate_names_and_bindings_*`
- [✓] schema incompatível (mapping ↔ campos reais da capability/tool, obrigatórios sem origem, transform inexistente, enum parcial, flow ↔ inputs da capability, enum de slot ↔ capability, `summary_template`, lookup com schema diferente) — `test_mapping_*`, `test_output_mapping_*`, `test_an_enum_mapping_*`, `test_a_flow_must_feed_*`, `test_a_flow_slot_cannot_offer_*`
- [✓] downgrade de risco — `test_a_binding_cannot_lower_the_risk`; entre versões, `test_lowering_a_capability_risk_is_a_breaking_change`
- [✓] bindings inválidos (write ambíguo mapeado como retryable/timeout, status HTTP impossível) — `test_an_ambiguous_write_cannot_be_mapped_to_a_retryable_error`
- [✓] versões incompatíveis (formato do manifest, `min_framework`, semver, regressão, mudança quebrante sem MAJOR) — `test_a_newer_manifest_format_is_refused`, `test_a_manifest_that_needs_a_newer_framework_is_refused`, `test_a_breaking_change_needs_a_major_bump`

Implementado: `core/definitions/schema_spec.py` (DSL serializável de schema → modelo Pydantic estrito; `schema_fingerprint` canônico para qualquer modelo),
`core/definitions/manifest.py` (`AgentManifest`: o mesmo modelo dos objetos Python onde já eram dado; só classes viram `ModelSpec`),
`core/compiler.py` (diagnósticos *coletados* com `code`+`path`; `CompiledAgent` com digest de conteúdo), `core/publishing.py` +
`core/versioning.py` (regras de publicação), `ports/registry.py` + `adapters/registry/{memory,postgres}.py` (migration 0007, imutável por trigger),
`adapters/manifest/yaml_loader.py` (`safe_load`, chaves duplicadas rejeitadas, limite de tamanho, `on:` não vira booleano),
`app/compile.py` (CLI para CI), pinning por versão no `TurnCoordinator` (`registry` + `agent_id` + `versioned_engine_factory`; `StoredConversation.agent_version`)
e reconciliation por versão (`ReconciliationWorker(registry=, agent_id=, pipeline_factory=)`). O agente de exemplo existe nos dois formatos
(`vertical_slice/agent.yaml` e `definitions.py`) e **compilam para o mesmo digest**; um teste roda o mesmo cenário nos dois.

### Decisões de arquitetura da Fase 5

1. **Manifest = mesmo modelo, não um segundo.** Bindings, mapping, flows, recovery, retry e textos de confirmação já eram Pydantic serializáveis e entram como estão; só `input_model/output_model` (classes) viram `ModelSpec`. Evita a divergência YAML × Python.
2. **Equivalência por conteúdo, não por forma.** `schema_fingerprint` ignora títulos/nomes de classe, inlina `$ref` e trata `required` como conjunto (o JSONB não preserva a ordem das chaves — bug real: o digest do manifest recarregado do banco mudava). `agent_document` exclui a *versão* do digest: mesmo conteúdo com outro rótulo tem o mesmo digest.
3. **Diagnósticos coletados.** Verificações de referência/risco/versão rodam sobre o manifest antes de construir; depois `AgentDefinition` (regras semânticas existentes, agora reportadas como `DEFINITION_INVALID`) e as checagens pós-construção, compartilhadas com agentes Python (`compile_agent`).
4. **Respostas de sistemas externos são lenientes** (`extra=ignore`) e o que o framework define é estrito (`extra=forbid`): uma API que adiciona um campo não derruba o agente.
5. **Write ambíguo é erro de definição, não só coerção em runtime**: o compiler exige `unknown` em `default_5xx/timeout` de escrita.
6. **Publicação imutável e monotônica**: mesma versão+mesmo digest = no-op, mesma versão+outro conteúdo = `VersionConflictError`, versão menor = `VersionRegressionError`, mudança quebrante (capability removida, risco menor, schema de entrada/saída mudou, flow removido) exige MAJOR — a versão é uma promessa para as conversas fixadas. No Postgres: lock advisory por agente (publicadores concorrentes) e trigger contra UPDATE/DELETE.
7. **O que se guarda é o manifest** (um grafo Python não é armazenável); ao carregar, recompila e confere o digest (`RegistryIntegrityError` se um compiler novo ou uma linha adulterada não bate). Agentes só-Python usam o registry em memória.
8. **Pinning**: a conversa fica na versão do primeiro turno enquanto houver flow, ação pendente ou turno retomado; ociosa, migra para a mais recente. Versão fixada ausente → o turno fica aberto (`retry_later`), nada roda em outra versão.
9. **Reconciliation usa a versão que PREPAROU a invocação** (`context.agent_version`): um deploy entre PREPARE e a reconciliation não vira mais handoff por `INTENT_CHANGED`; versão ausente → handoff `AGENT_VERSION_UNAVAILABLE`, sem adivinhar.

### Débitos conhecidos (Fase 5)

- Output/error mapping do lookup continua sendo lido do binding *atual* da versão que preparou (agora a versão certa, mas o fingerprint ainda congela só entrada e destino).
- Manifest só em YAML/JSON de arquivo; não há API de publicação nem promoção/rollback de "latest" (hoje `latest` = maior versão publicada).
- `agent_version` não participa do `ToolContext` de testes antigos que constroem agentes Python soltos (sem registry): modo single-version continua suportado.
- Dry-run de migração de conversas entre versões (simular o estado dos flows contra a nova definição) não existe; a migração só acontece com a conversa ociosa.
- Compatibilidade de *flows* entre versões é só "removido": mudança de slots/steps não é classificada.
- Os checks de `Choose.value_field` e fluxos olham schemas, não a semântica dos valores (ex.: um datetime sem fuso).


## Phase 5.1 — hardening pós-revisão da Fase 5

Status: **PASS** (achados verificados no código antes de corrigir). Suíte completa verde (ver contagem abaixo).

| Achado | Resolução | Teste |
|---|---|---|
| P0 compatibilidade de versões olhava o risco da *capability* | `breaking_changes` compara o envelope resolvido (`effective_risk`, `requires_protection`, confirmação efetiva) por capability | `test_a_minor_version_cannot_lower_the_effective_risk`, `test_a_minor_version_cannot_remove_effective_protection`, `test_raising_protection_in_a_minor_version_is_fine` |
| P0 pin só por versão | `conversation_states.agent_id` (migration 0008); pin `(agent_id, version)`; outro agente com flow/ação/turno em andamento → `AgentMismatchError` (turno fica aberto); ociosa → reatribuição explícita | `test_a_conversation_is_pinned_to_the_agent_not_just_the_version`, `test_an_idle_conversation_can_be_reassigned_*` |
| P0 INV-029 contornável (o engine aceitava `AgentDefinition`) | `TurnEngine` só aceita `CompiledAgent`, selado: só `compile_manifest`/`compile_agent` o constroem | `test_only_the_compiler_can_produce_a_compiled_agent` |
| P1 compiler só checava existência, não tipos | `core/compiler_types.py`: inferência de tipos (path → transform → alvo), `TRANSFORM_SPECS` com tipos de entrada/saída; `MAPPING_TYPE_MISMATCH`, `MAPPING_TRANSFORM_INPUT`, `FLOW_TYPE_MISMATCH`; coerções legítimas (datetime↔string ISO, int→number, enum→string) continuam válidas | `test_a_mapping_cannot_feed_a_value_of_the_wrong_type`, `test_a_transform_*`, `test_compatible_coercions_are_not_errors`, `test_a_flow_cannot_feed_a_capability_the_wrong_type` |
| P1 path continuava "dentro" de um escalar | `resolve_path`: `.key`/`[i]` em escalar/lista é `MAPPING_SCALAR_TRAVERSAL` | `test_a_mapping_cannot_walk_beyond_a_scalar` |
| P1 tool sem output alimentando capability com output | `TOOL_HAS_NO_OUTPUT` | `test_a_tool_without_output_cannot_feed_a_capability_that_needs_one` |
| P2 `compiler_version`/`schema_version` colunas mortas | o registry recusa versão de compilador não suportada (`RegistryCompatibilityError`) e confere `agent_id`/`version`/`schema_version` da linha contra o manifest carregado, além do digest | `test_the_row_identity_must_match_the_manifest_it_holds`, `test_an_agent_from_an_unknown_compiler_version_is_refused_explicitly` |

Aceito e documentado: o compiler ainda para entre estágios (versões/referências → construção → checagens de mapping/flow), então um manifest com
problemas em estágios diferentes não os reporta todos numa só passada; dentro de cada estágio tudo é coletado.
Política de compiler: `SUPPORTED_COMPILER_VERSIONS` é um conjunto; quando o compiler mudar de significado, a versão antiga precisa de um loader
compatível (ou o agente histórico é re-publicado) — até lá, falhar explicitamente é o comportamento escolhido.


## Phase 5.2 — fechamento da camada Definition → Compiler → Registry → Runtime

Status: **PASS** (achados verificados no código antes de corrigir). Novas invariantes **INV-030** e **INV-031**.

| Achado | Resolução | Teste |
|---|---|---|
| P0 protected `PendingAction` sem pergunta de confirmação (`confirmation_prompt_enabled: false`) | `AgentDefinition` rejeita `false` enquanto houver capability protegida permitida (vale para Python e manifest); permitido quando nada é protegido | `test_a_protected_agent_cannot_turn_the_confirmation_question_off`, `test_the_confirmation_question_can_be_off_when_nothing_is_protected` |
| P0 templates de confirmação sem `{summary}` / com placeholder desconhecido explodiam em runtime | `core/templates.py`: bem formado, nomes simples, sem format spec; `prompt`/`reprompt` exigem `{summary}`, `executed_fallback` só `{status}`, `rejected/expired/gave_up` nenhum; o mesmo vale para todo texto de Flow (slots + `{options}`/`{question}`) | `test_confirmation_texts_must_be_formattable_*`, `test_flow_texts_cannot_hold_templates_*` |
| P0 split-brain (`TurnEngine` com `CompiledAgent` A + `CapabilityPipeline` de B) | `CapabilityPipeline` é construído a partir do `CompiledAgent`; o engine confere versão+digest do pipeline e do `ConfirmationStage` (`ValueError` se diferirem); wiring usa o pipeline como raiz | `test_the_engine_refuses_a_pipeline_built_from_another_compiled_agent` |
| P1 type check só por categoria (list/object) | tags estruturais recursivas: `list[A]→list[B]`, objeto→objeto (obrigatórios providos, tipos compatíveis, alvo estrito não recebe campo extra); limite de profundidade para modelos recursivos | `test_a_whole_list_cannot_fill_a_list_of_different_items`, `test_an_object_cannot_fill_*`, `test_structurally_compatible_objects_are_accepted`, `test_a_strict_target_object_rejects_*` |
| P1 defaults de `FieldSpec` não validados | validados contra tipo/enum/limites/itens/objeto na construção do manifest e coagidos uma vez no `build_model` (Pydantic não valida defaults: um `date` ficava string); semântica de `required:false` ⇒ ausente e null aceitos, documentada | `test_a_default_must_satisfy_its_own_field`, `test_valid_defaults_are_accepted_and_applied`, `test_an_optional_field_accepts_an_explicit_null_as_documented` |
| P1 remoção da allowlist não era breaking | `allowed_capabilities` removida exige MAJOR | `test_removing_a_capability_from_the_allowlist_is_a_breaking_change` |
| P1 registry global vs tenant | **decisão: tenant-scoped** `(tenant_id, agent_id, version)` — API `publish/get/latest/versions` recebe `tenant_id`; migration 0009 recria a tabela (0007 nunca teve dados); lock advisory por `(tenant, agent)`; coordinator e reconciliation usam o tenant da conversa/invocação | `test_the_registry_is_tenant_scoped` |
| P1 `RegistryIntegrity/Compatibility` derrubava o worker | turno: ALERT + `retry_later` (nada executa); reconciliation: só aquela invocação vai a `HUMAN_HANDOFF` (`AGENT_VERSION_UNAVAILABLE`) e o lote segue | `test_a_corrupt_agent_stops_that_conversation_not_the_worker`, `test_a_corrupt_definition_hands_that_invocation_to_a_human_and_the_batch_goes_on` |
| P2 `min_framework` fora da identidade da publicação | `CompiledAgent.manifest_digest` (artefato publicado) separado de `digest` (o que o agente faz); mesma versão + outro manifest ⇒ `VersionConflictError` ("different metadata") | `test_two_manifests_that_differ_only_in_metadata_are_not_the_same_publication` |
| P2 armadilha do primeiro bump de compiler | `COMPILERS: {versão → função de compile}` em `core/compiler.py`; o registry escolhe o loader pela `compiler_version` gravada (o antigo continua existindo no bump) | `test_an_agent_from_an_unknown_compiler_version_is_refused_explicitly` |

Aceito: o compiler ainda reporta por estágio (já documentado na 5.1). Requisito explícito do primeiro bump de compiler: adicionar o
loader novo em `COMPILERS` mantendo o anterior (ou republicar o histórico).


## Phase 5.3 — a linguagem de mapping que o compiler aceita é a que o runtime executa

Status: **PASS** (achados reproduzidos antes de corrigir). 792 testes. **Fase 5 encerrada.**

| Achado | Resolução | Teste |
|---|---|---|
| Alta: `ToolRunner` validava a resposta da Tool e mapeava o JSON cru (valor coercível virava `BINDING_OUTPUT_MAPPING_FAILED`/`UNKNOWN` depois do side effect) | o output mapping lê `output_model.model_validate(...).model_dump(mode="json")`: coerção, defaults e descarte de campos não declarados valem também no runtime | `test_output_mapping_uses_the_validated_tool_response`, `test_a_coercible_valid_write_response_never_becomes_unknown`, `test_fields_the_tool_did_not_declare_never_reach_the_mapping` |
| Alta (A) `Const` perdia a estrutura (lista/objeto só por categoria) | `tag_of_value` recursivo | `test_a_constant_must_have_the_right_structure_*` |
| Alta (B) `Ref.default` ignorado | o default substitui o valor quando o path falta, então precisa caber no alvo (`MAPPING_TYPE_MISMATCH`) | `test_a_ref_default_must_fit_the_target_*` |
| Alta (C) enum map aceitava objeto/lista (todos eram `tuple`) | origem precisa ser `string` ou enum (`MAPPING_ENUM_SOURCE`) | `test_an_enum_map_only_reads_strings` |
| Alta (D) `[*]` tinha tipo diferente no compiler e no runtime | `resolve_path` carrega cardinalidade: `$.items[*].x` é `list[X]`, `[0]` é `X` | `test_a_wildcard_yields_a_list_exactly_like_the_runtime` |
| Alta (E) `Each` sobre lista de escalares pulava a validação | `Each` exige `list[object]` (`MAPPING_EACH_NEEDS_OBJECTS`); `Each` não preenche lista de escalares | `test_each_needs_a_list_of_objects`, `test_each_cannot_fill_a_list_of_scalars` |
| Média: manifest armazenado inválido lançava `CompileError` fora do contrato do registry | o adapter converte em `RegistryIntegrityError` (contido por conversa/invocação) | `test_a_stored_manifest_that_does_not_compile_is_an_integrity_error`, `test_a_corrupt_stored_manifest_stops_that_conversation_not_the_worker` (linha real no Postgres) |
| Média/baixa: `manifest_digest` não persistido | coluna `manifest_digest` (migration 0010, tabela recriada) conferida no load: metadado alterado com semântica igual é detectado | `test_changed_manifest_metadata_is_detected_even_when_the_semantics_match` |

Limpeza de artefato (`archive.zip`, `__pycache__`, `.pytest_cache`): o repositório já os ignora via `.gitignore`; o zip é gerado fora do git.


## Phase 5.4 — autoridade de tempo, headers/URL do HTTP e fechamento do type system do compiler

Status: **PASS** (achados reproduzidos antes de corrigir). Novas invariantes **INV-032** e **INV-033**.

| Achado | Resolução | Teste |
|---|---|---|
| Alta: o default efetivo da coordenação era o relógio local do worker (`coordination or CoordinationTime(db, clock)`) | `CoordinationTime(db)` é só `clock_timestamp()` e é o default de lease/UoW/ledger/outbox/scheduler; `FixedCoordinationTime(clock)` é o stand-in de teste, injetado explicitamente; `ports/coordination.py` define o contrato | `test_stores_default_to_the_database_clock_not_the_workers_clock` |
| Alta: `LeaseHandle.ensure_active` comparava `clock.now()` local com `expires_at` do banco | o lease carrega `observed_at` (hora da autoridade); a checagem estima o tempo da autoridade = leitura + tempo monotônico decorrido | `test_a_workers_skewed_wall_clock_*`, `test_a_lease_expires_by_elapsed_authority_time_*` |
| (achado adicional da mesma classe) backoff da reconciliation usava o relógio da aplicação para `due_at` consumido por quem lê o relógio do banco | `ReconciliationWorker` recebe `coordination` e agenda com ele | `test_reconciliation_backoff_is_a_coordination_timestamp` |
| P0 `AuthSpec.header` podia sobrescrever `Idempotency-Key`, `Host`, `X-*` | `RUNTIME_OWNED_HEADERS` + validação de nome; defesa em profundidade no adapter (`INVALID_AUTH_CONFIGURATION`, nada enviado) | `test_auth_cannot_name_a_runtime_owned_header`, `test_even_a_forged_auth_spec_never_overwrites_the_idempotency_key` |
| P0 `base_url` aceitava userinfo/query/fragment (fora do fingerprint) | só scheme+host+porta+path; credenciais vêm do `SecretProvider`, query do spec da tool | `test_base_url_is_only_scheme_host_port_and_path` |
| Alta: type checker perdia nulabilidade e uniões (`ANY`) | lattice com `nullable`, `union` e `null`: `str|null` não preenche campo não-nulo; valores heterogêneos viram união; caminho por objeto opcional é anulável; enum map/transform sobre origem anulável exigem `default`; **o runtime agora aplica `default` também a `null`** (um model dump escreve opcionais como null) | `test_an_optional_tool_field_cannot_fill_*`, `test_a_default_makes_a_nullable_source_safe`, `test_null_only_fits_a_nullable_target`, `test_values_of_different_types_are_a_union_*`, `test_a_transform_or_enum_map_cannot_read_a_nullable_source_*`, `test_the_runtime_falls_back_to_the_default_for_null_*` |
| Alta: `HTTPRequestSpec` não era conferido contra o input da tool (um argumento obrigatório podia nunca ser enviado) | `HTTP_UNKNOWN_ARGUMENT` e `TOOL_ARG_NOT_SENT` | `test_the_http_spec_must_send_every_required_tool_argument`, `test_the_http_spec_cannot_reference_arguments_*` |
| Média: `recovery.result_map` sem validação estática | checado como um binding (lookup output → output original) | `test_recovery_result_map_is_checked_like_a_binding` |
| Média: semântica `required × nullable × default` | documentada e testada: independentes; `required:false` sem default ⇒ ausente vira None (null aceito); com default e `nullable:false` ⇒ `{}`→default, null rejeitado | `test_a_required_field_with_a_default_rejects_an_explicit_null` |
| P2 descrição de campo contava como breaking no SemVer | `schema_fingerprint(docs=False)` (fingerprint de compatibilidade) | `test_documentation_changes_are_not_breaking_but_type_changes_are` |
| P2 versão corrompida no registry escapava como `DefinitionError` | `RegistryIntegrityError` | `test_a_corrupt_stored_version_label_is_an_integrity_error` |
| Limpeza do archive | `.gitignore` já cobre `__pycache__`, `.pytest_cache` e `archive*.zip` | — |

Nota de teste: o teste que prova que o runtime recusa um `result_map` inválido (`LOOKUP_RESULT_UNUSABLE`) agora constrói o pipeline com
`make_pipeline(..., unchecked=True)`, porque o compiler passou a recusar essa definição antes (defesa em profundidade, não substituição).


## Phase 5.5 — o que o binding produz chega ao fio; conexões inválidas são erro canônico

Status: **PASS** (reproduzido antes de corrigir). Suíte completa verde.

| Achado | Resolução | Teste |
|---|---|---|
| Alta: um argumento OPCIONAL produzido pelo binding podia não ser enviado pelo transporte HTTP (a API externa aplicaria o default dela: operação executada ≠ operação confirmada) | `MAPPED_TOOL_ARG_NOT_SENT`: todo argumento que algum binding da tool pode produzir precisa estar em path/query/body, ou ser declarado em `HTTPRequestSpec.ignored` (decisão explícita); `ignored` não pode esconder argumento obrigatório nem contradizer o spec | `test_a_mapped_optional_tool_argument_cannot_be_silently_dropped`, `test_an_optional_argument_can_be_declared_ignored_explicitly`, `test_ignored_cannot_hide_*`, `test_an_argument_no_binding_produces_may_stay_unsent` |
| P1: `base_url` com porta inválida (`:99999`, `:abc`) passava na construção e explodia no fingerprint | o validator avalia a porta (1–65535); defesa em profundidade: `InvalidConnectionError` → resultado pré-I/O `INVALID_CONNECTION_CONFIGURATION` no PREPARE (nada é preparado) e no envio | `test_a_malformed_port_is_refused_when_the_connection_is_built`, `test_an_invalid_connection_is_a_canonical_pre_io_error_never_a_raw_exception` |

Registrado, sem mudança: `status_lookup` é hoje *lookup por `idempotency_key`* (não genérico: referência externa/negócio fica para a Fase 7);
a compatibilidade SemVer é por igualdade de fingerprint (conservadora: adicionar campo opcional exige MAJOR) — direcionalidade fica para quando
a ergonomia incomodar.


## Phase 6 — RelayPlane + mídia + proativo

Status: **PASS contra o contrato publicado do RelayPlane** (`github.com/Ppaulo03/RelayPlane`, `docs/CONTRACT.md` no commit `880820a`; resumo em `docs/RELAYPLANE_CONTRACT.md`).
A primeira versão da fase assumia um contrato inventado; ao ler o repositório real, quatro premissas de segurança estavam erradas e a borda foi refeita:
não há consulta por chave (o replay da mesma chave é a primitiva); o envio é assíncrono (`202 QUEUED` + `message_id`, `provider_message_id`/`accepted_at` só no ACCEPTED);
`UNKNOWN` do gateway exige `resolve` humano; assinatura/envelope/headers (`X-RelayPlane-*`, `v1=`) e mídia (claim-check com `READY/REJECTED/FAILED`) são outros.
Tudo é testado contra um **simulador** (`examples/reference-relayplane`) fiel ao contrato publicado; `tests/contracts/test_relayplane_live.py` (opt-in, `RELAYPLANE_*`)
confere replay de chave e retenção (`GET /limits`) contra um gateway implantado — **ainda não executado** (sem credenciais neste ambiente).
Novas invariantes **INV-034, INV-035, INV-036**.

DoD (`ROADMAP.md` Fase 6):

- [✓] webhook persiste antes de 2xx — `test_a_signed_message_is_persisted_before_the_2xx`, `test_nothing_is_acknowledged_that_was_not_persisted` (503, nunca 2xx), `test_a_redelivery_is_acknowledged_without_a_second_row`
- [✓] HMAC — `test_unverifiable_requests_are_rejected_and_never_persisted`, `test_secret_rotation_accepts_either_signature`, `test_the_event_id_header_must_agree_with_the_signed_body`
- [✓] outbound sai somente da outbox — `OutboxWorker` é o único que importa `ports.sender` (teste de arquitetura); mapeamento de status em `result_from_gateway`
- [✓] HUMAN/HANDOFF_PENDING silenciosos — `test_a_conversation_the_bot_does_not_own_stores_context_and_says_nothing`, `test_a_human_owned_conversation_stays_silent_for_timers`
- [✓] timers/proativos sobrevivem a restart — `test_a_timer_survives_a_restart_and_sends_exactly_one_message`, `test_a_timer_fired_twice_is_one_event_and_one_message`
- [✓] `sender_retry_horizon <= idempotency_retention` — `DeliveryPolicy` + `test_the_retry_horizon_must_fit_inside_the_gateways_idempotency_retention`, `test_the_delivery_policy_is_checked_against_what_the_gateway_reports` (lê `/limits`); comportamento: `test_the_sender_does_not_resend_blindly_past_the_retry_horizon`, `test_past_the_gateways_retention_no_resend_is_safe_and_nothing_is_resent`, `test_after_its_retention_the_gateway_forgets_the_key_and_a_resend_duplicates` (por que o horizonte existe)
- [✓] claim-check/mídia + Transcriber — `tests/engine/test_media.py`, `tests/contracts/test_relayplane_media.py`, `test_media_arrives_as_a_reference_with_what_the_gateway_made_of_it`
- [✓] channel policy, handoff de ownership — `tests/postgres/test_proactive.py`, `tests/postgres/test_handoff.py`
- [ ] contract test contra o RelayPlane **real** — escrito (`test_relayplane_live.py`), não executado

Implementado: `core/models/delivery.py` (`DeliveryPolicy`), `core/models/media.py`, migrations 0011 (outbox `first_sent_at`/`reconcile_attempts`), 0012 (inbox `media`/`kind`) e 0013
(`outbox.channel_message_id`, `inbox.provider_message_id`), `adapters/senders/relayplane.py` (`send`/`lookup`/`resolve`/`delivery_policy`), `engine/outbox_reconciler.py`
(`claim_unsettled`), `OutboxWorker` com horizonte, `adapters/relayplane/{webhook,asgi,subscriptions,media}.py` (`message.received`, `message.outbound_status`, `message.deleted`),
`engine/media.py` + step `MEDIA_NORMALIZED` + `ports/transcriber.py` (+ fake), `engine/proactive.py` + `PROACTIVE_DECISION` + `ports/channel_policy.py` + `ServiceWindowPolicy`,
`engine/ownership.py`, `Handoff` de Flow → `HANDOFF_PENDING` na mesma transação.

### Decisões da Fase 6

1. **A janela da idempotency key é premissa de segurança**: reenviar a MESMA linha só enquanto o gateway lembra a chave (`retry_horizon <= retenção`, retenção lida de `/limits`); depois disso nada é reenviado às cegas (INV-034).
2. **Aceite ≠ entrega**: `QUEUED` guarda o `channel_message_id`; ACCEPTED (com `provider_message_id` e `accepted_at` do canal) chega por evento ou por consulta; o estado só avança.
3. **`UNKNOWN` do gateway nunca é decidido pela automação** (`CHANNEL_UNKNOWN_NEEDS_DECISION`; `RelayPlaneSender.resolve` é ação de operador).
4. **Tenant/instância do webhook vêm do cadastro da subscription**; o payload não escolhe; `schema_version` ≠ 1 é recusado.
5. **Transcrição é um step do journal** (replay reaproveita); falha/vazio/grande demais/`REJECTED`/`FAILED` viram uma linha de texto nomeada, nunca falha do turno; o runtime só nomeia mídia que não lê.
6. **Proativo reentra pelo runtime** como evento `kind=system` deduplicado pela chave do timer; `ChannelPolicy` decide e a decisão é journalada; sem política nada sai; não renova `last_event_at`.
7. **Regras de canal (janela de atendimento, silêncio) vivem no adapter**; o RelayPlane não tem templates nem janela de 24h.
8. **Ownership só muda sob o lease** da conversa; via Flow `Handoff`, na mesma transação da resposta.
9. `message.deleted` retira (READY → DEAD) só o que ainda não foi consumido por um turno; grupos e edições `secretEncrypted` são reconhecidos e ignorados.

### Débitos conhecidos (Fase 6)

- Contrato real **não executado** contra um gateway implantado (só contra o simulador fiel ao repositório).
- Sem detecção de lacuna em `sequence` (o gateway garante ordem sem lacunas; guardamos `source_sequence`, mas não alertamos), sem bootstrap/rotação automática de subscription e de `secret` (hoje `StaticSubscriptionResolver`).
- Respostas citando (`reply_to`) o `provider_message_id` de um prompt nosso ainda não são usadas pelo runtime (a evidência já é gravada na outbox).
- Mensagens **template** não existem (nem no RelayPlane): fora da janela a política nega. `defer` do `ChannelPolicy` não existe.
- Transcriber real (STT) não implementado (porta + fake); o download de mídia é `RelayPlaneMediaFetcher` (limite de tamanho + checksum), sem pinning de IP/SSRF (destino é configuração do operador).
- Rate limit/backpressure do envio ficam para a Fase 10; delivery/read receipts não fazem parte da garantia mínima.


## Phase 7 — Tool ecosystem

Status: **PASS** (suíte completa verde; mypy/ruff/import-linter limpos, 8 contratos). Novas invariantes **INV-037, INV-038, INV-039**.
Escopo do ROADMAP: MCPToolProvider, discovery + allowlists, OpenAPI importer opcional, ReplayToolProvider, métricas/circuit breakers por provider.

DoD (derivado do ROADMAP e do DESIGN §32/§37):

- [✓] MCPToolProvider sobre a mesma proteção de transporte do HTTP (conexão pré-registrada por tenant, HTTPS, allowlist de host, SSRF/DNS pinning, sem redirect, resposta limitada, credencial do SecretProvider) — `tests/contracts/test_mcp_provider.py` (29); o HTTP continua passando a mesma suíte
- [✓] discovery ≠ exposição; allowlist explícita com risco do operador — `tests/contracts/test_tool_import.py`
- [✓] schema do servidor pinado por digest; mudança trava a chamada antes de enviar — `test_a_changed_input_schema_stops_the_call_before_anything_is_sent`, `test_the_pin_is_rechecked_after_its_ttl_and_not_on_every_call`
- [✓] OpenAPI importer opcional (mesmas regras) — `test_an_allowed_operation_becomes_an_http_tool_with_its_own_transport`
- [✓] ReplayToolProvider (+ gravação com redaction), métricas e circuit breaker por (tenant, conexão) — `tests/contracts/test_tool_resilience.py`
- [✓] MCP no manifest/compiler sem alterar o digest de agentes existentes — `tests/compiler/test_mcp_manifest.py`

Implementado: `adapters/tools/guard.py` (`GuardedTransport`, extraído do HTTP: uma única cópia da proteção), `adapters/tools/mcp.py` (`MCPToolProvider`: sessão, schema pin, discovery,
classificação de erros), `core/definitions/json_schema.py` (JSON Schema → DSL do manifest, subset conservador, `schema_digest`), `core/definitions/tool_import.py`
(`DiscoveredTool`, `ToolAllowlist`, `import_mcp_tools` → `ToolManifest`), `core/definitions/openapi_import.py`, `MCPToolSpec` em `ToolDefinition`/manifest,
`ErrorMap.tool_error`, `adapters/tools/{replay,resilience,metrics}.py` + `ports/metrics.py`, `examples/reference-mcp` (servidor MCP de referência, sistema externo).

### Decisões da Fase 7

1. **MCP por Streamable HTTP, sem o SDK `mcp` e sem stdio.** O transporte HTTP já é o endurecido do projeto (SSRF, pinning, limites); o SDK traria um segundo cliente HTTP fora dessas garantias, e stdio (um processo por tenant) não é um transporte que o runtime consiga fazer fencing. JSON-RPC é pequeno e foi testado contra um servidor real.
2. **Discovery não é exposição (INV-037):** só entra o que a allowlist nomeia; o risco é SEMPRE decisão do operador (campo obrigatório) e dica do servidor (`readOnlyHint`/`destructiveHint`) só pode aumentar o cuidado: allowlist `read` contra um servidor que diz "não é read-only" exige `accept_server_hint_conflict`. Idem para OpenAPI (`GET` vs método que muda estado).
3. **Schema pinado:** o digest do `inputSchema` (sem documentação) vai em `MCPToolSpec`; antes da primeira chamada por sessão (e a cada `schema_check_ttl`) o provider compara com o schema atual: divergente ou ausente → `technical_error` sem enviar nada. Custo assumido: dentro do TTL uma mudança não é vista.
4. **Conversão de schema recusa em vez de aproximar:** `pattern`, `oneOf/allOf`, `const`, objetos livres, enums não-string etc. são erro nomeado; todos os problemas são listados de uma vez. Descrições do servidor são cortadas/limpas (prosa de terceiro).
5. **Resultado de MCP é dado:** `isError`/erro de protocolo seguem a taxonomia canônica: o que prova não-execução (params inválidos, método desconhecido) é seguro; `isError` ou erro interno de ESCRITA é `unknown` (pode ter executado parcialmente), salvo `error_map.tool_error` explícito, que nunca torna uma escrita retryable. O texto de erro do servidor não é repassado.
6. **Circuit breaker é transparente (INV-038):** só barra ANTES de chamar o provider (`CIRCUIT_OPEN`, `technical_error` retryable = não enviado, seguro até para escrita); tudo que o provider responde passa inalterado. Só saúde conta (inalcançável, timeout, 5xx, resposta inutilizável, `unknown`); resposta de negócio/validação prova que está vivo e erro de configuração não é culpa do destino. Estado em memória, por processo.
7. **Replay não tem transporte (INV-039):** chamada não gravada é `REPLAY_MISS`, nunca resposta inventada; respostas repetidas saem na ordem gravada e depois acabam; a gravação redige PII por padrão.
8. **Compatibilidade de digest:** `mcp` e `error_map.tool_error` só entram no documento do agente/fingerprint quando presentes; agentes e intenções preparadas antes desta fase mantêm o mesmo digest/fingerprint.

### Débitos conhecidos (Fase 7)

- Sem stdio, sem SDK `mcp`, sem recursos/prompts/sampling de MCP: só `tools/list` e `tools/call`; conteúdo não-texto (imagem, resource) é ignorado.
- Sem `OpenAPIToolProvider` dedicado nem gRPC (o importador gera tools HTTP comuns); OpenAPI: só path/query + corpo JSON objeto, sem header/cookie, sem `oneOf`.
- Circuit breaker e métricas são por processo e em memória; backend real de métricas/dashboards e estado compartilhado ficam para a Fase 10.
- Breaker e `MeteredToolProvider` não vêm ligados por padrão em nenhuma wiring (composição explícita pelo operador).
- Sem CLI de discovery/import (as funções existem; falta um comando `tools import`).
- Idempotência de MCP: o header `Idempotency-Key` e a declaração `idempotency_supported` são do operador; o MCP não padroniza isso.


## Phase 8 — Primeiro Pack

Status: **PASS** (suíte completa verde; mypy/ruff/import-linter limpos, 9 contratos). Novas invariantes **INV-040, INV-041**.
ROADMAP: extrair `generic-scheduling` do comportamento já provado; o mesmo Pack deve funcionar com uma segunda API usando bindings/mappings diferentes.

DoD:

- [✓] o Pack é o comportamento provado, extraído — `test_installing_the_pack_reproduces_the_agent_the_slice_proved` (mesmos contratos de capability, mesmo Flow, mesmas tools e bindings que o `agent.yaml` da Fase 5)
- [✓] o mesmo Pack, uma segunda API de vocabulário/semântica diferentes (`examples/reference-agenda-api`: `servico/de/ate`, UTC, `duracao_min` em minutos, page tokens, 422 em vez de 409, outros códigos e outro fuso) — `examples/pack-hosts/{clinic,studio}.yaml`; nenhuma linha do Pack muda
- [✓] evals do Pack passam nos dois hosts contra as APIs reais — `test_every_eval_of_the_pack_passes_on_each_host_against_its_real_api`
- [✓] protocolo completo nos dois hosts (proposta → confirmação → escrita preparada → execução → reconciliação por lookup do próprio host) sobre PostgreSQL — `tests/postgres/test_pack_hosts.py`
- [✓] depois da compilação o agente é autocontido: publica/recarrega no registry sem o Pack, com procedência em `pack_lock`
- [✓] sem acesso privilegiado: o Pack não carrega tools/bindings/connections/secrets e não amplia o que o modelo pode chamar

Implementado: `core/definitions/pack.py` (`PackManifest`, `PackUse`, `PackLock`, `ParameterSpec`, `BindingRequirement`, `EvalScenario`), `core/packs.py` (`expand_packs`, parâmetros,
colisões, requisitos), `AgentManifest.packs`/`pack_lock`, `compile_manifest(raw, packs)`, `ports/pack_loader.py` + `adapters/manifest/pack_loader.py` (`DirectoryPackLoader`),
`app.compile --packs DIR`, `engine/evals.py` (`run_scenario`), `packs/generic-scheduling/pack.yaml`, `examples/reference-agenda-api`, `examples/pack-hosts/`.

### Decisões da Fase 8

1. **Pack é dado, instalado antes do compilador (INV-040):** `expand_packs` transforma o manifest (puro, sem I/O) e o runtime nunca vê um Pack. Versão exata (`name` + `version`), `min_framework` verificado, duplicata/inexistente são erros.
2. **Um Pack contribui definições, não acesso:** capabilities, flows, fragmentos de persona, requisitos e evals; `extra=forbid` não deixa lugar para tools/bindings/connections/secrets/allowlist.
3. **O Pack nunca amplia o que o modelo chama:** suas capabilities só ficam expostas pelo `allow` explícito do agente (o Flow do Pack precisa delas liberadas, ou a compilação falha, em vez de abrir sozinho).
4. **Nunca sombreia nem sobrescreve:** colisão de nome de capability ou flow (com o agente ou com outro Pack) é erro `PACK_NAME_COLLISION`; o risco/confirmação do Pack é piso (binding só sobe, o compilador recusa descer).
5. **Requisitos checados na instalação:** capability ligada, tool idempotente, estratégia de recuperação aceita, lookup pelo capability certo (`PACK_REQUIREMENT_UNMET`) — o que a escrita idempotente e a reconciliação supõem deixa de ser convenção.
6. **Parametrização mínima e tipada:** o que só o agente sabe (catálogo de serviços, pergunta de serviço, gatilhos extras) é `parameters` validado pelo mesmo DSL de schema e substituído por nós `{"$param": nome, "project": campo}` (`$keys` = chaves, `splice` = emenda em lista). Referências a parâmetros não declarados falham ao carregar o Pack.
7. **Procedência separada de identidade:** o agente compilado guarda o manifest já instalado (`packs` vazio, `pack_lock` com nome/versão/digest); `digest` (o que o agente faz) não muda com o texto do Pack, `manifest_digest` (o artefato publicado) muda.
8. **Evals são dados do Pack, o runner é do framework:** cenários com expectativas sobre resposta/proposta e captura de trechos da resposta; `eval_variables` são as palavras que só o agente conhece. Julgam a conversa, não o estado do sistema externo.

### Bug encontrado pela segunda API (corrigido)

O Flow só comparava o horário pedido ("às 16h") com as **5 opções exibidas**, não com tudo que a tool devolveu: com a API de estúdio (slots a partir das 08:00) "às 11h" respondia "não tenho exatamente esse horário" havendo 11:00 livre. Agora a preferência (e uma hora dita durante a escolha) casa com todas as opções reais; número/ordinal continuam referindo só ao que foi mostrado (`FlowInstance.unlisted`). Testes em `tests/engine/test_flows.py`.

### Débitos conhecidos (Fase 8)

- Pack em pt-BR, único idioma; `policies` do manifesto conceitual do DESIGN §33 não existem como definição no framework e por isso não estão no Pack.
- Sem registry de Packs (só `DirectoryPackLoader`; "registry de Packs" segue em aberto no DESIGN), sem faixas de versão (versão exata) e sem assinatura/verificação de origem.
- Evals não rodam o LLM (o Pack é determinístico via Flow); o harness completo é da Fase 10.
- O texto de persona do Pack é anexado ao do agente sem checagem de contradição com a persona dele.
