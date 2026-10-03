# IMPLEMENTATION_STATUS

Fonte de verdade: `docs/INVARIANTS.md` > `docs/RUNTIME_PROTOCOL.md` > `docs/DESIGN.md` > `docs/ROADMAP.md`
(os arquivos no repositório não têm o sufixo `_v4`).

**Fase atual:** 1 — Vertical slice (concluída; Fase 2 não iniciada).

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

Testes (155 passam, 2 live deselecionados; sem LLM real no CI):

| Área | Arquivo | # |
|---|---|---|
| architecture | `tests/architecture/test_boundaries.py` | 51 |
| contract LLMProvider | `tests/contracts/test_llm_provider_contract.py` | 15 |
| contract ToolProvider | `test_tool_provider_contract.py`, `test_http_tool_provider.py` | 19 |
| mapping / error mapping / binding | `test_mapping.py`, `test_error_mapping.py`, `test_binding_and_runner.py` | 41 |
| engine (INV-002/003/013, replay INV-014/017/018) | `tests/engine/*` | 20 |
| GO/NO-GO e2e | `tests/evals/test_phase1_go_nogo.py` | 9 |
| live (marker `integration`) | `tests/integration/test_live_anthropic.py` | 2 (skipped sem chave) |

Qualidade: `ruff check` + `ruff format --check` verdes; `mypy --strict src examples` verde; `lint-imports` 6/6 contratos.

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
- `scheduling.create` tem tool/binding definidos, mas `POST /bookings` não existe na API de referência (Fase 2).
- `TOOL_RESULT` no journal é provisório: na Fase 2 o fato externo passa a viver no ToolInvocation ledger
  (`execution_epoch`); o journal referenciará a invocation.
- `INBOUND_AGGREGATED` por turno único: sem Inbox/burst/ordering (Fase 2).
- Resolução de datas relativas fica a cargo do LLM com o `turn_reference_time` no prompt; normalização determinística
  pt-BR é Fase 4 (DESIGN §22).
- `mypy` cobre `src` e `examples`; `tests/` não é type-checado (imports de `conftest`/`support`).
- Retry de leitura e `recovery` strategies (Fase 3) não implementados; `ToolDefinition` ainda sem `retry`/`idempotency`.
- Histórico sem sumarização (apenas janela `max_history_messages`).

### Questões encontradas

- **Live**: a suíte live (`uv run --env-file .env pytest -m integration`) passou com Groq; consome no máx. ~6 chamadas e tem
  orçamento diário local (`LIVE_LLM_DAILY_CALLS`, padrão 20, `.live_llm_usage.json`; 429 zera o dia). Anthropic segue validado contra o contrato com client do SDK stubado e tipos reais do SDK;
  `tests/integration/test_live_anthropic.py` (`uv run pytest -m integration`) cobre o contrato com a API real e uma sonda
  de qualidade (o modelo só oferece slots que a API tem). **Rode-o antes de fechar o GO definitivo**; é o único item do
  GO/NO-GO que não foi executado.
- Nenhuma contradição normativa entre os documentos foi encontrada; as lacunas (representação do "draft" na Fase 1,
  posição de `tools/` nas camadas) foram resolvidas como decisões locais acima.
