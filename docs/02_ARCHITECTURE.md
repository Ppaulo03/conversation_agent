# 02 — Architecture (como o sistema funciona agora)

Descreve o estado atual, sem histórico. O porquê está em [`03_DECISIONS`](./03_DECISIONS.md); o normativo detalhado está em
[`INVARIANTS.md`](./INVARIANTS.md) e [`RUNTIME_PROTOCOL.md`](./RUNTIME_PROTOCOL.md) (continuam sendo a especificação
fina; este documento é o mapa). Onde este texto e o código divergirem, vale o código/testes e a divergência vira finding em
[`05_REVIEW_LOG`](./05_REVIEW_LOG.md). Fontes verificadas: árvore `src/`, `pyproject.toml` (contratos import-linter),
migrations `0001`–`0025` e a suíte em `tests/`.

## 1. Módulos e fronteiras

`src/conversation_agent/`:

| Pacote | Papel |
|---|---|
| `core/` | Modelos tipados e regras puras: definições (`AgentDefinition`, capabilities, tools, bindings, flows), `compiler`, `packs`, `versioning`/`publishing`/`releases`, `retention`, orçamento e preços de LLM, redaction, observability/tracing (contexto), `temporal_ptbr`, `http_path`, hashing canônico. Sem I/O. |
| `ports/` | Protocolos (Protocol) para tudo que sai do processo ou do relógio: `llm`, `tool_provider`, `uow`, `inbox`, `outbox`, `ledger`, `lease`, `scheduler`, `coordination` (tempo), `sender`, `transcriber`, `media_fetcher`, `registry`, `releases`, `audit`, `budget`, `ratelimit`, `admission`, `metrics`, etc. |
| `tools/` | Linguagem de mapping restrita, mapeamento de erros, risco efetivo, construção de `CapabilityRequest`/intent. |
| `engine/` | Orquestração: `TurnEngine` (agent loop), `TurnCoordinator`, `CapabilityPipeline`, `PolicyGate`, `ToolRunner` (único que chama `ToolProvider`), confirmação (`ConfirmationStage`), side effects, flow runner, reconciliação (tools e outbox), outbox worker, scheduler worker, heartbeat, ownership, proactive, evals. |
| `adapters/` | Implementações: `postgres/` (uow, inbox, outbox, ledger, lease, scheduler, coordenação, migrator, retenção, integridade, rate limit, health), `llm/` (Anthropic, OpenAI-compatible incl. Groq, fake, replay, metered), `tools/` (HTTP, MCP, guard/transporte, resilience, replay, fake, chaos), `relayplane/` (webhook, subscriptions, media), `senders/`, `transcribers/`, `registry/`, `manifest/` (YAML), `observability/` (logs, Prometheus, ASGI `/healthz /readyz /metrics`, SLO), `ratelimit/`, `audit/`, `secrets/`, `connections/`, `policies/`, `journal/`. |
| `app/` | Composição e CLIs: `runtime.Runtime.build`, `serve`, `migrate`, `compile`, `evals`, `ops`, `usage`, `integrity`, `observability`, `llm_factory`, `stt_factory`, `wiring`, `cli`. |

Fora de `src/`: `examples/` (sistemas externos de referência e agentes de exemplo, só dado/código de teste), `packs/`
(`generic-scheduling`), `ops/` (artefatos gerados de `ops/slo.yaml`), `tests/`.

### Direção de dependências (impostas por 9 contratos import-linter)

```text
core  ←  ports  ←  tools  ←  engine          (camadas; nunca ao contrário)
adapters  →  core + ports                      (adapters nunca importam engine nem app)
core/ports/tools/engine ✗ adapters, app, SDKs externos (httpx, anthropic, openai, …)
src/ ✗ examples/* (sistemas externos de referência)
```

Vocabulário de domínio (agendamento, suporte) nunca vive em `src/` (teste de arquitetura de INV-008).

## 2. Runtime principal

`app.runtime.Runtime.build(db, compiled, llm, providers, sender, …)` monta o grafo durável a partir de **um único
`CompiledAgent`** (INV-030): `receive(event)` grava no inbox; `run(stop)`/`tick()` aciona os workers. Workers: `TurnCoordinator`
(turnos), `OutboxWorker`, `OutboxReconciler`, `ReconciliationWorker` (ledger de tools), `SchedulerWorker` (timers/proativos).
`app.serve` é o entrypoint de desenvolvimento (terminal como canal); em produção o operador compõe o `Runtime` com o sender do
RelayPlane e o app ASGI do webhook.

## 3. Modelo de estado e de execução

- **Estado conversacional** (`ConversationState`): histórico, flows ativos, propostas, handles de mídia (`media_N`, só referências),
  dono (BOT/HANDOFF_PENDING/HUMAN), `scope`, `outbound_seq`. Nunca guarda estado de negócio.
- **Turno**: candidato → lease da conversa → claim dos eventos *só dessa conversa* → rajada (com `debounce` opcional) →
  `turn_reference_time` imutável → gate de ownership → confirmação / flow ativo / router → capability → policy → PREPARE →
  execução externa → finalize no ledger → aplicar à conversa → compor → outbox → consumir inbox → soltar lease
  (algoritmo: `RUNTIME_PROTOCOL.md` §12).
- **Journal de turno**: cada step LLM/tool/decisão é gravado com `(turn_id, step_index, step_type)` + `request_hash`; replay reaproveita
  e **diverge fail-closed** (`JournalDivergenceError`) (INV-014/017/018).
- **Nenhuma transação de banco permanece aberta durante LLM ou HTTP externo.**

## 4. Persistência

PostgreSQL é o único estado durável; toda "fila" é uma tabela. Schema por migrations forward-only (`0001_init` … `0025_llm_usage_audio`),
checksum por arquivo, apply serializado por advisory lock; banco à frente do código ou migration editada impedem iniciar (INV-047).
`ConversationUnitOfWork` agrupa a escrita local; linhas publicadas de agente são imutáveis no próprio banco (INV-028);
`admin_audit` é append-only por trigger (INV-043).

## 5. Inbox / Outbox

- **Inbox**: dedupe por `(tenant, channel, event_id)` (INV-020); webhook só responde 2xx depois de assinatura HMAC verificada e evento
  persistido (INV-035); backpressure recusa *antes* de persistir (INV-046).
- **Outbox**: todo outbound sai da outbox durável (INV-007); estados `PENDING/SENDING/QUEUED/ACCEPTED/FAILED/UNKNOWN/RECONCILING/SUPERSEDED`;
  mesma linha ⇒ mesma `Idempotency-Key`; ordem por conversa via `conversation_seq` (INV-065); reenvio cego só dentro do horizonte
  da `DeliveryPolicy`, que não pode alargar a janela de idempotência do gateway (INV-034/061/066). `provider_accepted_at` é do
  domínio do canal ou NULL (INV-026).

## 6. Ledger de tools (ToolInvocation)

`PREPARED → EXECUTING → SUCCEEDED | FAILED | UNKNOWN → RECONCILING → RECONCILED | HUMAN_HANDOFF`. O PREPARE congela a *intent*
(args mapeados, provider, fingerprint de conexão/binding, contrato de recovery) e nada além dela é enviado depois (INV-023/027).
Protocolo A/B/C1/C2 em `RUNTIME_PROTOCOL.md` §4. `UNKNOWN` é reconciliado por `status_lookup`, `retry_same_key` (exige
idempotência), `safe_retry` (só read) ou `human_handoff`; nunca retry cego (INV-006/021). Resultado pendente não é falha do turno (INV-025).

## 7. Scheduler

Timers duráveis (proativos, `reconcile:<invocation_id>`, expirações) em tabela própria, claimados por worker e relógio do banco;
sobrevivem restart; proativo respeita `ChannelPolicy` e silêncio sob HUMAN (INV-036/019).

## 8. Leases e fencing

Dois fences independentes: `conversation_epoch` (estado, turno, confirmação, journal, outbox gerada) e `execution_epoch` (execução e
finalização de uma `ToolInvocation`). Fencing de conversa = `SELECT … FOR SHARE` condicionado a (owner, epoch) no começo de toda UoW;
takeover exige lock exclusivo da mesma linha; lease expirado não autoriza escrita mesmo antes de takeover e heartbeat não o
ressuscita (INV-009/011/016/024). Perder o lease da conversa não impede o executor válido de gravar o fato externo no ledger (C1);
o novo dono aplica C2. Lease/claim/backoff usam o relógio do banco (`CoordinationTime`), nunca o do worker (INV-032).
Cancelamento é cooperativo e só em safe boundary (INV-012). Runtimes que compartilham banco são isolados por `scope` (INV-057).

## 9. Confirmação e semântica de side effects

Capability de risco ≠ read (efetivo, via `ResolvedToolBinding.requires_protection`) vira `PendingAction` com `action_id` determinístico
e `args_hash`. Prompt de confirmação é texto do runtime, entregue pela outbox; confirmação só vale se o prompt está `ACCEPTED` e
a resposta é provadamente posterior (reply-to, ou timestamps do mesmo domínio com tolerância de skew; sem prova ⇒ re-pergunta)
(INV-022/026). “Sim, mas…” modifica e invalida; alteração de args invalida (INV-010). `ActionConfirmation + CONFIRMED + PREPARED` são
atômicos (INV-005). Handoff invalida ações pendentes e retira prompts não enviados na mesma transação (INV-060). Depois do efeito, a
resposta não contradiz o resultado (`executed_template`/`executed_fallback`, INV-056).

## 10. Modelo de agente e versões

`AgentManifest`/YAML + allowlist → `compile_agent` → `CompiledAgent` (selado; só o compiler o produz, INV-029) → registry
tenant-scoped `(tenant, agent_id, version)` com versões imutáveis e digest (INV-028/031). `ReleaseManager` decide quem recebe qual versão
(stable/canary/withdrawn); promover exige evidência de eval para o digest exato; conversa com trabalho em andamento nunca migra
(INV-044). Campos opcionais entram no digest só quando usados. Packs são dado instalado *antes* do compilador e só contribuem
definições (INV-040).

## 11. Adapters externos

- **LLM**: contrato canônico (`LLMRequest/Response`); Anthropic, OpenAI-compatible (Groq/OpenAI/local), Fake, Replay; wrapper `metered`
  grava uma linha por chamada real no ledger de uso (INV-050).
- **Tools**: HTTP e MCP compartilham o transporte guardado (`GuardedTransport`: DNS fixado, rede privada recusada, TLS, sem redirect;
  INV-033/062/070); MCP só importa o que a allowlist nomeia (INV-037); circuit breaker/rate limit falham *antes* de enviar (INV-038/046).
- **RelayPlane**: contrato resumido em [`RELAYPLANE_CONTRACT.md`](./RELAYPLANE_CONTRACT.md) (outbound assíncrono 202/QUEUED, sem consulta
  por chave; inbound webhook HMAC; mídia por referência com sha256). Existe um simulador em `examples/reference-relayplane`.
- **Transcriber** (OpenAI-compatible, opt-in por operador *e* agente, INV-067) e `MediaFetcher` (INV-070).

## 12. Observabilidade

Logs JSON com contexto fechado de campos, spans como linhas de log (`trace_id` nasce no webhook e é persistido), métricas Prometheus
em memória por processo + gauges de backlog/idade das filas lidos do banco, `/healthz /readyz /metrics`, SLOs em `ops/slo.yaml`
(de onde alertas e dashboard são gerados), ledger de custo de LLM append-only com preços como dado e orçamento por tenant.
Não há exportador OpenTelemetry nem agregação entre processos (finding F-005).

## 13. Invariantes arquiteturais principais

Resumo — a lista completa e o mapa INV → teste estão em `INVARIANTS.md`: INV-001 (fronteiras), INV-002/013 (LLM não é autoridade),
INV-003 (todo efeito cruza o gate), INV-005/022 (confirmação provada e atômica), INV-006/021 (sem retry cego), INV-007 (outbox),
INV-008 (sem estado de negócio), INV-009/011/016/024/032 (fences e tempo), INV-014/017 (replay fail-closed), INV-023/027 (intent
congelada), INV-028/029/030 (agente imutável, compilado, raiz única), INV-047 (migrations), INV-048/049 (observabilidade lateral e sem conteúdo).

Prova executável: 16 chaos gates (`C01`–`C16`, tabela em `ROADMAP.md`, exigidos por `tests/architecture/test_chaos_gates.py`).
