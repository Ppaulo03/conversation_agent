# Roadmap e gates — conversation_agent v4

Fonte única das fases, DoD, GO/NO-GO e chaos gates. Detalhes arquiteturais ficam em [`DESIGN.md`](./DESIGN.md) e [`RUNTIME_PROTOCOL.md`](./RUNTIME_PROTOCOL.md); invariantes em [`INVARIANTS.md`](./INVARIANTS.md).

Cada fase só avança quando seu DoD e os chaos gates atribuídos a ela estão verdes (tabela em "Chaos gates").

## Escopo crítico da v1

Incluído:

- CLI + FastAPI/RelayPlane;
- PostgreSQL;
- 1 LLM provider real + Fake/Replay;
- HTTPToolProvider;
- Capabilities + Bindings + mappings + `error_map`;
- Python Flow model;
- PendingAction + ActionConfirmation + PolicyGate;
- Inbox + Outbox + Turn Journal + Ledger + Scheduler;
- conversation lease + fencing + heartbeat;
- split `conversation_epoch` / `execution_epoch`;
- reference-scheduling-api;
- OpenTelemetry básico;
- chaos tests.

Não bloqueiam v1:

- MCP completo;
- OpenAPI importer;
- Pack registry;
- FAQ/RAG;
- vector memory;
- embeddings para intent;
- streaming;
- Flow YAML genérico;
- plugins Python de terceiros não confiáveis.

## Fase 1 — Vertical slice

Objetivo: provar valor conversacional antes do reliability core, sem persistência sofisticada, DSL ou confirmação protegida.

```text
CLI -> Turn -> LLM -> Capability read -> Binding -> HTTP tool -> reference API -> response
```

Inclui:

- objetos Python tipados;
- `ToolContext` confiável;
- Capability de leitura (`scheduling.availability`);
- Binding com input/output/error mapping;
- FakeLLM + um provider real;
- contrato interno de LLM;
- contrato de Turn Journal + implementação in-memory para replay local;
- FakeToolProvider/HTTPToolProvider;
- testes de fronteira arquitetural.

DoD:

- conversa multi-turn;
- FakeLLM e provider real passam o mesmo contrato;
- availability real via HTTP;
- input/output/list mapping;
- error mapping canônico;
- journal in-memory;
- replay de LLM journaled sem nova chamada;
- engine sem SDK/provider concreto.

### GO/NO-GO

Antes da Fase 2, avaliar cenários reais do caso guia. O protected write ainda não é executado; a conversa deve chegar de forma confiável até a **proposta estruturada de agendamento pronta para confirmação**.

Cenários mínimos:

- agendamento simples;
- consulta e escolha entre slots reais da API de referência;
- indisponibilidade + alternativa;
- correção de data/hora;
- falha HTTP de leitura sem resposta enganosa;
- conversa chega a `BookingDraft`/`CapabilityRequest` válida para `scheduling.create`;
- evals mostram qualidade suficiente para justificar a Fase 2.

**GO:** abstrações e UX mostram valor sem mudanças de domínio em core/engine.

**NO-GO:** corrigir modelagem conversacional/capability/binding antes de investir em reliability core.

## Fase 2 — Reliability core

Implementar:

- schema PostgreSQL v1;
- `ConversationUnitOfWork`;
- Inbox/Outbox;
- Turn Journal;
- ToolInvocation ledger;
- Scheduler;
- conversation lease + fencing + heartbeat;
- execution lease/epoch;
- ordering/late events;
- reconciliation worker;
- fault injection.

Nesta fase podem existir writes idempotentes da API de referência para provar prepare/execute/finalize, **sem afirmar ainda suporte completo a protected actions conversacionais**.

DoD:

- duplicate inbound não cria turn duplicado;
- claim de eventos acontece somente depois do conversation lease;
- outbox/scheduler sobrevivem restart;
- worker zumbi não altera conversation state;
- executor válido consegue finalizar o ledger após perder o conversation lease;
- heartbeat impede expiração silenciosa durante LLM/tool longa; perda do heartbeat torna o worker stale;
- journal evita repetir LLM step persistido;
- journal divergente falha fechado;
- `turn_reference_time` é estável no replay;
- `UNKNOWN` entra em reconciliation;
- nenhum I/O externo mantém transaction da conversa aberta;
- **chaos C04–C11 e C14–C16 passam**.

## Fase 3 — Protected actions + confirmation + security

Implementar:

- `PendingAction`;
- `ActionConfirmation`;
- `ConfirmationInterpreter`;
- atomic `CONFIRMED + PREPARED`;
- canonical `args_hash` e `action_id` lifecycle;
- invalidação em modificação;
- elegibilidade do prompt por `latest_prompt_outbox_id`, Outbox `ACCEPTED`, reply-to/timestamp do canal e skew tolerance;
- re-prompt de UNKNOWN e limite de re-prompt;
- effective risk;
- retry compiler rules;
- PolicyGate completo;
- guardrails operacionais;
- HTTPToolProvider hardened;
- ConnectionResolver/SecretProvider (SSRF/secrets).

DoD:

- protected action nunca executa sem prompt elegível + confirmação válida;
- “sim” anterior ao prompt ou ambíguo não confirma;
- “sim, mas...” invalida a ação anterior;
- CONFIRMED nunca existe sem PREPARED após commit;
- FAILED terminal exige nova tentativa semântica (novo `action_id`);
- timeout/5xx de write ambíguo vira UNKNOWN, nunca retry cego;
- retry de write sem idempotência é rejeitado pelo compiler;
- **chaos C01–C03 e C13 passam**.

## Fase 4 — Flows e entendimento

Implementar:

- Flow model em Python tipado;
- active-flow-first routing;
- ownership gate antes do Router;
- slot extraction híbrida e validação determinística;
- correção de slot;
- digressão/suspend-resume;
- cancelamento em safe boundaries;
- transitions de erro/timeout/unknown;
- datas/horários/duração pt-BR (normalização temporal determinística).

DoD:

- flow corrige slot sem perder contexto válido;
- digressão permitida retorna ao flow;
- `cancel_requested` não interrompe external call em voo;
- HUMAN não executa Router/LLM automático;
- todos os outcomes canônicos têm transição/default seguro;
- **chaos C12 passa**.

## Fase 5 — AgentDefinition + Compiler

Formalizar serialização/configuração somente depois de provar objetos Python:

- AgentDefinition formal e CompiledAgent;
- YAML/manifest schema;
- compiler (binding/mapping/`error_map` validation);
- versioning e publicação imutável.

DoD: o compiler detecta referências ausentes, schema incompatível, downgrade de risco, bindings inválidos e versões incompatíveis antes do runtime.

## Fase 6 — RelayPlane + mídia + proativo

- subscriptions/inbound adapter;
- MessageSender;
- HMAC;
- claim-check/mídia;
- Transcriber;
- timers proativos;
- channel policy;
- handoff ownership.

DoD: webhook persiste antes de 2xx; outbound sai somente da outbox; HUMAN permanece silencioso; timers/proativos sobrevivem restart.

Inclui contract test da janela `relayplane_idempotency_retention` e verificação:

```text
sender_retry_horizon <= relayplane_idempotency_retention
```

## Fase 7 — Tool ecosystem

- MCPToolProvider;
- discovery + allowlists;
- OpenAPI importer opcional;
- ReplayToolProvider;
- métricas/circuit breakers por provider.

## Fase 8 — Primeiro Pack

Extrair `generic-scheduling` do comportamento já provado. O mesmo Pack deve funcionar com uma segunda API usando bindings/mappings diferentes.

## Fase 9 — Segundo domínio

FAQ/suporte/ticket, sem alterar core/engine.

## Fase 10 — Production hardening

- eval harness completo;
- rollout/rollback;
- migration tooling;
- retenção/LGPD operacional;
- dashboards/SLOs;
- capacity/backpressure;
- backup/DR;
- auditoria administrativa.

## Fase 13 — Uso real (serve, pacote instalável, canal de terminal)

Achados de um POC que montou o runtime durável a partir de helpers de teste.

- **`serve`:** entrypoint oficial do runtime durável (Postgres, `TurnCoordinator`, `ConfirmationStage`, `OutboxWorker`, polling), uma única composição em `src/` que os testes também usam;
- **CLI que conclui:** o terminal como canal do `serve` (sender de console com dedupe por idempotency key e `provider_accepted_at`; eventos com `provider_occurred_at`), de modo que proposta, confirmação e execução funcionem de ponta a ponta;
- **pacote instalável:** o wiring do vertical slice dentro do pacote; `examples/` só com dados;
- **extras:** `[postgres]`, `[anthropic]`; modo em memória e outros providers sem puxar o que não usam;
- **docs de uso:** guia do runtime completo, incluindo o requisito de timestamps de canal para confirmação e o motivo da ambiguidade (hoje o contato só vê "não entendi");
- **depois (fase própria):** `choose` com mais de um campo, fallback de execução com campos do output, testes de caminho sem LLM/cancelamento/multi-worker.

## Fase 14 — Achados do POC (conversa, templates, multi-agente)

Do POC de agendamento de quadras (lista consolidada, abertos após a fase 13).

- **Corretude:** correção de slot durante o `choose` (feito); resposta pós-execução que contradiz o resultado (modo determinístico ou instrução própria); dois runtimes no mesmo banco roubando turnos (escopo por agente/deployment);
- **Templates e Flows:** `executed_fallback` com campos do output e por capability; formatação de data/hora no `summary_template`; `choose` com rótulo e mais de um campo e auto-seleção; período do dia ("à noite");
- **Pequenos:** gatilho de Flow que sequestra perguntas; diagnóstico de empate de gatilhos; separação entre digressão e pergunta do Flow; `contact_id` para a API externa;
- **Cobertura:** caminho sem LLM, cancelamento, reconciliação de outbox sob falha, multi-worker.

## Fase 15 — Mídia de ponta a ponta

> 15a (transcriber real) e 15b (mídia como argumento de tool) implementadas; 15c (visão opcional) por vir.

Hoje só o áudio é lido (transcrição por step journalado); imagem, vídeo e documento são apenas nomeados, e nenhuma tool recebe a mídia.

- **Transcriber real:** adapter de provedor (Whisper/Groq ou equivalente) atrás da porta `Transcriber`, com custo no ledger de uso e teto por agente;
- **Visão opcional, por agente:** imagem enviada ao modelo por referência (nunca bytes no estado/journal), journalada, com custo e orçamento; sem o opt-in o comportamento e o digest atuais permanecem;
- **Mídia como argumento de tool:** referências da conversa guardadas com handle estável (`media_1`), limitadas, cobertas por retenção/erasure; o agente vê o handle na linha de texto; tool/slot declara argumento do tipo `media`; o runtime valida que o handle pertence à conversa (INV-002) e resolve para a referência no momento da chamada (o `MediaFetcher` busca os bytes);
- **Ação protegida com anexo:** proposta/confirmação continuam valendo e o sha256 entra na chave de idempotência;
- **Fora do escopo:** mídia de saída (o agente continua enviando só texto).

## Futuro (sem fase atribuída)

- **Grupos, menções e allowlist de contatos:** desenho em [`FUTURE_GROUPS.md`](./FUTURE_GROUPS.md); pedido ao
  gateway em [`RELAYPLANE_GROUPS_SPEC.md`](./RELAYPLANE_GROUPS_SPEC.md). Hoje grupos são ignorados.

## Chaos gates

Fonte única dos casos de fault injection. `C06`, `C07` e `C16` seguem o protocolo C1/C2 de [`RUNTIME_PROTOCOL.md`](./RUNTIME_PROTOCOL.md) §4:
**C1** grava o fato externo no ledger (`execution_epoch`); **C2** aplica o resultado à conversa (`conversation_epoch`).

| ID | Fase | Ponto de falha | Resultado esperado |
|---|---|---|---|
| `C01_before_confirm_commit` | 3 | antes da transação de confirmação | ação continua pendente |
| `C02_inside_confirm_prepare_tx` | 3 | depois de escrever `ActionConfirmation`, antes de inserir `PREPARED`, sem commit | rollback total; nunca existe `CONFIRMED` órfã |
| `C03_after_confirm_prepare_commit` | 3 | depois de `CONFIRMED + PREPARED` commitados | recovery encontra a mesma invocation |
| `C04_before_external_request` | 2 | depois de `PREPARED/EXECUTING`, antes do envio | retry/recovery seguro conforme contrato |
| `C05_after_external_send` | 2 | request enviado, antes da resposta | write ambíguo -> `UNKNOWN`/reconciliation |
| `C06_after_external_success` | 2 | sucesso externo recebido, antes do commit de **C1** | mesma idempotency key/reconciliation; sem duplicar |
| `C07_after_ledger_finalize` | 2 | **C1** commitado (fato no ledger, `result_application_status=pending`), antes de **C2** | novo owner/reconciliation aplica C2 sem repetir a tool; o contato recebe a resposta |
| `C08_during_outbox_send` | 2 | payload enviado, antes de `mark_sent` | mesmo payload + mesma idempotency key |
| `C09_worker_zombie` | 2 | lease expira e outro worker assume | worker antigo falha o fencing check |
| `C10_after_llm_response` | 2 | resposta LLM journaled, antes do próximo step | replay reutiliza `LLM_RESPONSE`, sem nova cobrança |
| `C11_during_reconciliation` | 2 | kill depois de claimar a reconciliação, antes de persistir a resolução | novo worker reassume por epoch; nenhuma execução cega |
| `C12_cancel_during_external_call` | 4 | `cancel_requested` com `ToolProvider` em voo | a chamada termina e o ledger é finalizado; a conversa para na próxima safe boundary |
| `C13_confirm_while_prompt_ambiguous` | 3 | inbound “sim” com prompt `SENDING/QUEUED/UNKNOWN` | nenhuma ação executa; confirmação inelegível; runtime reconcilia/re-prompta |
| `C14_lease_lost_after_external_success` | 2 | conversation lease perdido depois do sucesso externo (sem kill) | executor registra o fato por `execution_epoch` (C1); não altera a conversa; o novo owner aplica C2 |
| `C15_journal_hash_divergence` | 2 | replay com `request_hash` diferente do journal | `JournalDivergenceError`, turno falha fechado com alerta; nada é reexecutado |
| `C16_after_apply_before_compose` | 2 | **C2** commitado, antes de compor/persistir a Outbox | replay reproduz o journal; a tool não repete; a Outbox é criada uma única vez |

Assertions principais:

- reexecução não duplica side effect protegido;
- worker obsoleto não realiza commit na conversa;
- nenhum outbound desaparece depois de commit;
- evento duplicado não cria turno duplicado;
- `unknown` nunca vira retry cego;
- confirmação antiga não autoriza args modificados;
- LLM journaled não é chamado novamente no replay do mesmo step;
- `FAILED` terminal não reabre a mesma invocation.
