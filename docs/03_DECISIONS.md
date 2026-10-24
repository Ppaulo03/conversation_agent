# 03 — Decisions

Registro de decisões arquiteturais **duráveis e já demonstráveis** (código + teste). Formato da
[metodologia §25](./01_METHODOLOGY.md). Regras desta versão:

- Cada ADR cita a evidência (invariante/teste/seção do status legado). **O racional só reproduz o que está escrito nos documentos
  históricos** ([`DESIGN.md`](./DESIGN.md), [`IMPLEMENTATION_STATUS.md`](../IMPLEMENTATION_STATUS.md), [`RUNTIME_PROTOCOL.md`](./RUNTIME_PROTOCOL.md)); onde o
  racional não está registrado, o ADR diz isso em vez de inventá-lo.
- Alternativas só aparecem quando o material legado as menciona.
- A iteração Baseline & Engineering Consolidation **formalizou** decisões existentes; a única decisão nova dela é de governança
  documental (ADR-020). Nenhuma decisão de runtime foi criada.
- ADR-022 é posterior ao aceite dessa iteração: decisão de fronteira do produto, só documentação, sem alterar o comportamento do runtime.
- ADR-023 (F-010) altera o runtime: política de entrega obrigatória e semântica genérica de entrega.
- Descrição do estado atual: [`02_ARCHITECTURE`](./02_ARCHITECTURE.md).

---

## ADR-001 — Arquitetura hexagonal com fronteiras verificadas por ferramenta
Status: Accepted
Context: DESIGN §2/§46 fixam Ports & Adapters; o engine deve ser testável sem SDK nem banco.
Decision: `core ← ports ← tools ← engine`; adapters dependem de `core + ports`; engine/core/ports/tools não importam adapters, `app` nem SDKs
(`httpx`, `anthropic`, `openai`, …); `src/` nunca importa `examples/`. Imposto por 9 contratos import-linter e `tests/architecture/`.
Alternatives: framework LLM de terceiros (LangChain/LlamaIndex) foi descartado como dependência obrigatória (DESIGN §2: agent loop próprio, pequeno e controlado).
Consequences: nova integração = novo adapter; vocabulário de domínio fica fora de `src/` (INV-001/008).
Follow-ups: nenhum.

## ADR-002 — O framework guarda estado conversacional, nunca estado de negócio
Status: Accepted
Context: DESIGN §1.1; o caso guia (agendamento) não pode virar dependência do framework.
Decision: INV-008. Sistemas externos são a verdade do negócio; o runtime guarda só dado conversacional (lista fechada de campos testada em `test_runtime_does_not_own_business_state`).
Alternatives: não registradas.
Consequences: efeitos externos exigem ledger + reconciliação (ADR-005); mídia entra como referência, nunca bytes (INV-036/068).
Follow-ups: nenhum.

## ADR-003 — Dois fences: `conversation_epoch` e `execution_epoch`
Status: Accepted
Context: RUNTIME_PROTOCOL §1: perder o lease da conversa não apaga um fato externo já ocorrido.
Decision: `conversation_epoch` cerca estado/turno/confirmação/journal/outbox; `execution_epoch` cerca execução e finalização da `ToolInvocation`. Protocolo A/B/C1/C2: C1 grava o fato (executor válido, mesmo sem lease da conversa), C2 aplica à conversa (dono atual). Fencing = `SELECT … FOR SHARE` em (owner, epoch) no início de toda UoW.
Alternatives: não registradas além do caso “um único epoch”, que o protocolo existe para evitar (perderia o fato externo).
Consequences: INV-009/011/016/024; chaos C09, C14, C07, C16.
Follow-ups: nenhum.

## ADR-004 — O relógio do banco é a autoridade de coordenação
Status: Accepted
Context: Fase 5.4 / INV-032: comparar o relógio do worker com timestamps do banco permite lease “expirado” ou “vivo” incorretamente.
Decision: lease, claim, backoff, `available_at` do outbox usam `CoordinationClock` (`clock_timestamp()` em produção). `Clock` fica só para tempo conversacional.
Alternatives: relógio do worker (rejeitado pelo defeito acima).
Consequences: testes PostgreSQL usam relógio fixo para o conversacional e o do banco para coordenação (cuidado registrado em `AGENTS.local.md`).
Follow-ups: nenhum.

## ADR-005 — `UNKNOWN` é estado de primeira classe; nunca retry cego; intent congelada no PREPARE
Status: Accepted
Context: DESIGN §13, INV-006/021/023/027.
Decision: timeout/5xx de write após possível envio ⇒ `UNKNOWN`. Retry de write só com idempotência explícita e a **mesma** chave (o compilador recusa o resto). O PREPARE congela args mapeados, provider, fingerprint de conexão/binding e contrato de recovery; B, retry e reconciliação executam essa intent; divergência falha fechado sem enviar. Reconciliação por `status_lookup | retry_same_key | safe_retry | human_handoff`.
Alternatives: não registradas.
Consequences: um deploy entre PREPARE e envio não muda a operação; chaos C04–C06, C11.
Follow-ups: ver F-002 (garantias dependem do gateway real).

## ADR-006 — Turnos journalados com replay fail-closed
Status: Accepted
Context: INV-014/017/018; DESIGN §20A.
Decision: step identificado por `(turn_id, step_index, step_type)` + `request_hash`; replay reutiliza LLM/tool persistidos; divergência aborta com alerta; `turn_reference_time` imutável.
Alternatives: não registradas.
Consequences: não há dupla cobrança de LLM em replay (INV-050); decisões de confirmação e transcrição também são steps journalados.
Follow-ups: nenhum.

## ADR-007 — Confirmação provada pelo domínio do canal, não pelo relógio local
Status: Accepted
Context: INV-022/026; RUNTIME_PROTOCOL §7.
Decision: só prompt `ACCEPTED` é elegível; evidência por `reply_to_provider_message_id` ou timestamps do mesmo domínio com tolerância de skew; sem prova ⇒ ambíguo ⇒ re-pergunta nova (`outbox_id` novo). `provider_accepted_at` nunca é preenchido com horário local. Confirmação + `PREPARED` atômicos.
Alternatives: usar o horário local como evidência foi explicitamente excluído (INV-026).
Consequences: canais sem timestamp/reply-to deixam “sim” sem efeito (documentado em `GUIDE.md`).
Follow-ups: nenhum.

## ADR-008 — Outbox durável com idempotency key por linha; horizonte de reenvio ≤ retenção do gateway
Status: Accepted
Context: INV-007/034/061/065/066; o RelayPlane não oferece consulta por chave (`RELAYPLANE_CONTRACT.md`). Histórico: antes do contrato publicado, o `RUNTIME_PROTOCOL.md` registrava que a janela de idempotência outbound não estava formalizada; hoje o gateway a expõe em `GET /api/v1/limits`.
Decision: todo outbound sai da outbox; a mesma linha reenvia com a mesma chave só dentro de `sender_retry_horizon <= idempotency_retention`; depois, `UNKNOWN` sem chamada à rede; `UNKNOWN` do gateway só sai por decisão humana. Ordem por `conversation_seq`. A autorização de reenvio do reconciliador tem prazo próprio (`resend_authorized_until`).
Alternatives: apertar para `retry_horizon <= safe_resend_until` foi considerado e **não adotado** porque o contrato documentado é `<= retention` (status Fase 14.1).
Consequences: segurança de entrega depende de uma premissa externa ainda não evidenciada em produção.
Follow-ups: F-002.

## ADR-009 — Agente como dado compilado, versionado de forma imutável
Status: Accepted
Context: INV-028/029/030/031/044; Fase 5.
Decision: manifest → `compile_agent` → `CompiledAgent` selado (única raiz do grafo de runtime); versão publicada imutável com digest; registry tenant-scoped; release stable/canary exige evidência de eval do digest exato; conversa com trabalho em andamento nunca migra. Campos opcionais só entram no digest quando usados.
Alternatives: não registradas.
Consequences: domínio novo = manifest + allowlist + bindings sem tocar `src/` (Fase 9); acrescentar campo muda digest e fingerprints de intents já preparadas, por isso a regra “só quando presente”.
Follow-ups: nenhum.

## ADR-010 — Observabilidade é canal lateral com contexto fechado; tracing sem backend
Status: Accepted
Context: Fase 11, INV-048/049.
Decision: contexto via `contextvars` com conjunto FECHADO de campos; falha de logger/tracer/métrica/ledger nunca altera o trabalho; spans viram linhas de log estruturadas com `trace_id` persistido do webhook em diante. Exportador OpenTelemetry **adiado** como adaptador futuro sobre a interface `Tracer`.
Alternatives: dependência de backend de tracing no caminho quente (não adotada).
Consequences: sem OTel e sem agregação entre processos hoje (F-005).
Follow-ups: F-005.

## ADR-011 — Preço de LLM é dado, e modelo sem preço é “não precificado”
Status: Accepted
Context: Fase 11 decisões 3–6; INV-050.
Decision: `ops/llm_prices.yaml` entregue vazio de propósito; custo = tokens × preço do dia da chamada, aplicado na leitura; gasto contado por chamada REAL ao provider; custo por turno (não total) para comparar versões.
Alternatives: não registradas.
Consequences: até o operador preencher preços, tudo aparece como `unpriced` (limite inferior).
Follow-ups: nenhum.

## ADR-012 — Migrations forward-only com checksum
Status: Accepted
Context: Fase 10 decisão 6; INV-047.
Decision: sem scripts down; voltar = restore ou migration nova (expand/contract); drift ou banco à frente impedem aplicar (e a verificação os rejeita, ADR-026; nada no framework impede o host de iniciar sem verificar); apply serializado por advisory lock.
Alternatives: scripts down (descartados).
Consequences: restore é a única volta atrás de schema (relevante para F-003).
Follow-ups: F-003.

## ADR-013 — Orçamento de LLM excedido: `degrade` como padrão
Status: Accepted
Context: INV-071; decisão do usuário registrada em `IMPLEMENTATION_STATUS.md` (“Orçamento de LLM: degrade”).
Decision: opt-in por orçamento; `on_exceed ∈ {alert, degrade, refuse_new}`, padrão `degrade` (aceita e grava a mensagem, avisa uma vez por período, não chama o modelo; trabalho em andamento termina). Orçamentos já salvos mantêm o valor gravado.
Alternatives: `alert` e `refuse_new` continuam disponíveis; `refuse_new` pode perder mensagem se o gateway desistir (INV-051).
Consequences: mensagens durante o período excedido não entram no histórico nem são respondidas depois.
Follow-ups: nenhum.

## ADR-014 — Provedor de transcrição é configuração; transcrição desligada por padrão
Status: Accepted
Context: Fase 15a, INV-067; decisão combinada com o usuário.
Decision: um adaptador OpenAI-compatible cobre Groq/OpenAI/servidor local; precisa de operador (transcriber configurado) **e** agente (`transcription: on`); sem isso o áudio é só nomeado e nem baixado.
Alternatives: adaptadores por provedor (não adotados).
Consequences: dado de voz só sai do sistema com opt-in duplo.
Follow-ups: nenhum.

## ADR-015 — Mídia como argumento de tool por handle da própria conversa
Status: Accepted
Context: Fase 15b, INV-068; decisão do usuário: handle vale enquanto durar a retenção, limite 10 por conversa.
Decision: o modelo nomeia `media_N`; o runtime resolve para a referência (checksum) no momento da chamada; handle de outra conversa ou id do canal é recusado; só HTTP no corpo; bytes buscados na hora do envio.
Alternatives: não registradas.
Consequences: confirmação inclui o checksum; erasure leva os handles.
Follow-ups: nenhum (visão e MCP-com-arquivo são capacidades candidatas, não follow-ups comprometidos).

## ADR-016 — Sinais de saúde = backlog e idade das filas no banco
Status: Accepted
Context: Fase 10 decisão 1.
Decision: toda espera é uma tabela; `HealthSnapshot` lê backlog/idade com o relógio do banco, em vez de instrumentar o caminho quente.
Alternatives: instrumentação no caminho quente (a Fase 11 acrescentou métricas de turno depois, sem substituir isto).
Consequences: qualquer worker responde o mesmo; a idade do mais antigo denuncia worker morto/lease preso.
Follow-ups: F-005.

## ADR-017 — Backpressure recusa antes de persistir ou reconhecer
Status: Accepted
Context: INV-046; Fase 10 decisão 2.
Decision: 429/503 com `Retry-After` antes de gravar; só mensagens novas do usuário são recusadas, eventos de status/exclusão passam; limite de tool recusa antes de enviar (escrita negada = não-execução conhecida).
Alternatives: não registradas.
Consequences: o custo é latência, nunca dado, para canal at-least-once.
Follow-ups: nenhum.

## ADR-018 — Erasure conservadora com tombstones sem conteúdo
Status: Accepted
Context: INV-045; Fase 10 decisões 4–5.
Decision: bloqueadores nomeados impedem apagar; `force` só cancela o cancelável; linhas necessárias a dedupe/idempotência/ledger ficam sem conteúdo nem identificador; referências a pessoas em auditoria são HMAC com chave por deployment (INV-063).
Alternatives: SHA-256 simples foi substituído porque telefone/CPF eram enumeráveis offline (auditoria externa pós-fase 14).
Consequences: ver `OPERATIONS.md` (LGPD).
Follow-ups: nenhum.

## ADR-019 — Packs só contribuem definições; o primeiro Pack só depois do comportamento provado
Status: Accepted
Context: INV-040; DESIGN §1.3; Fase 8.
Decision: Pack é dado instalado antes do compilador, nunca lido pelo runtime, sem tools/bindings/connections/secrets; extraído de comportamento comprovado (`generic-scheduling`), validado com uma segunda API de vocabulário diferente.
Alternatives: Pack obrigatório desde o primeiro agente (rejeitado em INVARIANTS.md).
Consequences: segurança do que o modelo pode chamar não é ampliada por Pack.
Follow-ups: nenhum.

## ADR-020 — Hierarquia de autoridade da documentação
Status: Accepted (decisão de governança documental desta iteration; não altera runtime)
Context: três documentos legados davam ordens de precedência diferentes e dois se declaravam “fonte única” (F-004).
Decision: ordem de autoridade, do mais para o menos autoritativo: **código e testes** > `INVARIANTS.md` > `RUNTIME_PROTOCOL.md` > documentos canônicos `00`–`06` (cada um dono do seu assunto: projeto, arquitetura, decisões, roadmap, findings, handoff) > `DESIGN.md` (racional histórico) > `IMPLEMENTATION_STATUS.md` (log histórico). `ROADMAP.md` legado deixa de ter autoridade sobre fases (histórico), mas **mantém autoridade sobre a tabela de chaos gates** até ela ser movida (ver F-004). Conflito entre `INVARIANTS`/`RUNTIME_PROTOCOL` e o código ⇒ defeito a registrar em `05_REVIEW_LOG`.
Alternatives: manter a regra “INVARIANTS > RUNTIME_PROTOCOL > DESIGN > ROADMAP” de `IMPLEMENTATION_STATUS.md` (insuficiente: não trata os novos canônicos nem o ROADMAP defasado).
Consequences: documentos legados recebem cabeçalho de status (histórico/normativo) e deixam de reivindicar “fonte única” onde isso é falso.
Follow-ups: F-004.

## ADR-021 — `asyncpg` como driver PostgreSQL
Status: Accepted
Context: aprendizado registrado em `AGENTS.local.md`: o psycopg async não roda no ProactorEventLoop do Windows.
Decision: `asyncpg` (extra `[postgres]`); jsonb volta como dict/str conforme codec.
Alternatives: psycopg async (descartado pelo motivo acima).
Consequences: nenhuma, além do cuidado com codec jsonb nos testes.
Follow-ups: nenhum.

## ADR-022 — Framework embeddable sem deployment runtime canônico
Status: Accepted
Context: `conversation_agent` fornece um runtime durável como biblioteca (`Runtime.build`, workers, ports e adapters), mas pode ser consumido em ambientes e modelos de processo diferentes. Definir servidor, ASGI host, container, cloud, topologia ou deployment padrão acoplaria o framework a decisões que pertencem à aplicação operadora. O Product Owner definiu que o produto é um framework/módulo reutilizável de chatbot.
Decision: `conversation_agent` é um framework embeddable. Ele pode fornecer: runtime primitives; lifecycle APIs; ports; adapters opcionais; composition APIs; migrations e verificações de integridade; health/readiness primitives; métricas, tracing hooks e outras superfícies de observabilidade; contratos operacionais; exemplos de integração. Ele **não** possui deployment runtime canônico. Não são responsabilidade do framework: servidor/process manager de produção; ASGI hosting canônico; Docker/Kubernetes/serverless padrão; cloud provider; quantidade/topologia de processos; autoscaling; ingress/TLS; distribuição de segredos; provisionamento de PostgreSQL; backup/restore operacional; RPO/RTO; backend de métricas/tracing/logs; CI/CD da aplicação consumidora. A aplicação consumidora é o composition root operacional. `app.serve` continua sendo ferramenta de desenvolvimento/referência e não deve evoluir implicitamente para um deployment runtime oficial. PostgreSQL é o persistence backend canônico do durable runtime, não um modelo de deployment.
Alternatives: (1) fornecer um deployment runtime oficial — rejeitado: criaria ownership operacional fora do objetivo do framework; (2) não documentar nenhuma responsabilidade operacional — rejeitado: cada consumidor reconstruiria implicitamente os contratos necessários para operar o framework com segurança.
Consequences: o framework documenta o que fornece, o que o consumidor deve fornecer e quais invariantes operacionais o consumidor precisa respeitar (ex.: o PostgreSQL restaurado deve permanecer compatível com `migrate check` e `verify_integrity`; `DeliveryPolicy.retry_horizon` deve permanecer dentro da garantia de idempotência do canal (janela segura de reenvio, `idempotency_retention - safety_margin`; ver ADR-023); o lifecycle do consumidor deve permitir shutdown compatível com os workers do framework). A escolha e a operação da infraestrutura pertencem ao consumidor. Resolve a questão antes aberta sobre um entrypoint oficial de produção (F-006). Evidência de contrato do adapter RelayPlane (F-002) e CI do próprio framework (F-007) continuam responsabilidade do projeto.
Follow-ups: a próxima iteration avalia a Integration Readiness do framework (não cria um deployment padrão).

## ADR-023 — Explicit generic delivery safety policy
Status: Accepted
Context: F-010 (origin: Integration Readiness IR-004). A `Runtime` could be built without a `DeliveryPolicy` (`delivery_policy=None`), and then retried without a bound (`outbox_retry_horizon=None`) and did not reconcile. The generic engine worker, the PostgreSQL outbox store and the port carried RelayPlane's 24 h idempotency window as a default (`DEFAULT_UNKNOWN_BLOCKS_FOR`); `retry_horizon <= retention` ignored the safety margin; a retryable `FAILED` that ran out of retry time became `UNKNOWN`; a policy demanded a `lookup` even for channels that settle by pushed status; `QUEUED` could exist without a handle.
Decision:
1. `Runtime.build` requires an explicit `delivery_policy: DeliveryPolicy`; `None` never means "no safety". `outbox_retry_horizon` is removed (it only duplicated policy state).
2. One policy per Runtime, valid for every outbound delivery it handles. Heterogeneous channels/tenants: a conservative policy valid for all, or separate runtimes. No per-tenant dynamic policy.
3. Validation: `idempotency_retention > 0`, `0 <= safety_margin < retention`, `0 <= retry_horizon <= safe_resend_window = retention - safety_margin`. No resend, however authorised, leaves the safe resend window; lookups are not resends.
4. `retry_horizon == 0` is the canonical "never send this delivery again" (`DeliveryPolicy.no_retry`); no tiny positive value emulates it. The console channel (in-memory dedupe) uses it.
5. Every resend of one outbox row uses the same `idempotency_key` and payload; a new key is a new logical delivery.
6. Generic `SendResult` semantics: `ACCEPTED` terminal success; `QUEUED` non-terminal, always carries `channel_message_id` (rejected otherwise), never resent; `UNKNOWN` genuine ambiguity, non-terminal; `FAILED` the channel proves the delivery did not happen (`retryable=false` terminal; `true` retries with the same key inside `retry_horizon`, the safe window and the attempt cap, then ends `FAILED`, never `UNKNOWN`). An exception out of `send` is `UNKNOWN`. A stale `SENDING` is ambiguous (not the proven `FAILED`): resent per policy, else `UNKNOWN`. The claim reports what the row was (`claimed_from`, not stored) so the worker keeps the two apart without a schema change.
7. Reconciliation reuses `MessageLookup` (now `runtime_checkable`; `Runtime.build(lookup=...)`, default: the sender when it satisfies the protocol). Lookup and pushed status (`apply_channel_status`) are alternative settlement mechanisms; push-only is a valid composition, and a handled `QUEUED`/`UNKNOWN` may stay unsettled awaiting push (health/unsettled observability is the signal). `UNKNOWN` without a handle is resent with the same key only when `retry_horizon > 0`, inside the window; otherwise it stays `UNKNOWN`.
8. No generic RelayPlane default: the 24 h window is RelayPlane's own (`RelayPlaneSender.delivery_policy` reads it from `/limits`); generic code receives `safe_resend_window` from the policy.
9. No generic manual-resolution API: operator resolution stays adapter-specific (`RelayPlaneSender.resolve`).
Alternatives: keep `delivery_policy` optional with a default (rejected: an implicit policy is an unbounded one); per-tenant policy (out of scope); add `lookup()` to `MessageSender` or capability metadata to prove a settlement path at build time (out of scope; the integration contract states it instead); a schema column for the provenance of a retry (not needed: the claim carries it).
Consequences: intentional source/API breaking changes (no compatibility layer): `Runtime.build` requires `delivery_policy`; `outbox_retry_horizon` removed; `DeliveryPolicy.reconcile_margin` renamed `safety_margin` and `safe_resend_until` renamed `safe_resend_window`; `OutboxWorker` is built with `policy` and `coordination` (no `retry_horizon`/`unknown_blocks_for`); `OutboxStore.claim_ready` takes a required keyword `unknown_blocks_for`; `claim_unsettled` gains `with_handle`; `SendResult(QUEUED)` without `channel_message_id` is invalid. Upgrade note: no first-party sender ever produced a durable `QUEUED` row without a handle (RelayPlane always records the gateway id; console and fake never return QUEUED unless a test scripts it), so only a custom sender could have; such a legacy row is never claimed by the reconciler (it stays observable as `QUEUED`) and can only be settled by an operator. A channel that can answer non-terminal must offer lookup, push, or both, which the framework cannot prove at build time. The per-row attempt cap (store `max_attempts`) still applies and is not configurable here. No migration.
Follow-ups: none for F-010 (RESOLVED).

## ADR-024 — Generic ToolProvider side-effect safety contract
Status: Accepted
Context: F-011 (origin: Integration Readiness IR-008). `ToolRunner` retries a write when the tool is `idempotency_supported` with `same_idempotency_key` and the provider returns a retryable `technical_error`; it trusts the provider's classification, and the ledger records any `technical_error`/`timeout` as a final `FAILED` that reconciliation never revisits. The rules that make this safe (the stable invocation identity reaches the external system; an ambiguous write is `unknown`) were enforced only inside the HTTP/MCP adapters (`tools/error_mapping.py`), and the `ToolProvider` port did not state them. A custom provider could therefore duplicate a write or hide an ambiguous effect while the framework looked correctly configured.
Decision:
1. `ToolContext.invocation_id` is the logical identity of a tool invocation (the ledger's `idempotency_key` is the same value). The first attempt, in-process retries, reconciliation resends and recovery after restart/replay receive the same value. No second generic identity (`request_id`, `retry_token`, another key field) exists.
2. When retry/recovery relies on external idempotency, the provider binds that identity to the external system's dedupe/replay mechanism. The mechanism is the provider's (the HTTP/MCP adapters use `Idempotency-Key`; that is adapter-specific, not generic). A deterministic transport encoding of the identity is allowed (same external identity every attempt, no per-attempt randomness); a fresh identity per attempt is not.
3. `idempotency_supported` on a non-read tool is an assertion by the operator/provider that the external operation really deduplicates/replays, for every resend the tool's `RetryPolicy` and `RecoverySpec` may perform. The framework cannot verify it, and no idempotency-retention field is added; the obligation is documented.
4. Write classification (effective risk other than `read`): provable non-execution -> `technical_error`; known domain refusal -> `business_error`/`validation_error`/`policy_denied`; request possibly delivered with the effect unprovable -> `unknown`, always, never `technical_error`/`timeout`; idempotency support does not change that.
5. `technical_error` with `retryable=true` on a write means the attempt is known to have produced no effect and the retry rules allow another attempt; the runner still also requires `idempotency_supported` and `same_idempotency_key`.
6. `unknown` is never retried by `ToolRunner`; it stays under the frozen `RecoverySpec` (`retry_same_key`, `status_lookup`, `human_handoff`).
7. Providers do not raise for expected execution failures. If one does, the runner stays defensive (write -> `unknown`; read -> non-retryable `technical_error`); this is a contract violation, not an API.
8. Wrappers/decorators preserve the inner classification and produce a failure of their own only when they can classify it (e.g. a breaker rejecting before any request: a proven pre-send `technical_error`). INV-038 holds.
9. A custom `error_map` rule is an operator assertion held to the same contract: a retryable `technical_error` on a write requires proof of non-execution; an ambiguous outcome is `unknown`.
10. No new provider abstraction: no signature change, no proof marker, no capability metadata, no shared helper (the duplicated one-line key assignment in the two adapters shows no drift).
Alternatives: a runtime guard that downgrades a write's `technical_error` to `unknown` (rejected: the runner cannot tell proven non-execution from ambiguity without a new proof marker, an API change); an `idempotency_key` field on `ToolContext` (rejected: one logical effect, one identity); a tool-level idempotency-retention field (out of scope; documented as an obligation); a public conformance framework (rejected; focused repository tests suffice).
Consequences: a contract tightening with no signature change and no behavior change in the runtime or the first-party providers. A custom provider that mints a fresh dedupe identity per retry, claims `idempotency_supported` without binding the identity, reports an ambiguous write as `technical_error`/`timeout`, or sets `retryable=true` without proof of non-execution must change; a correct one needs nothing. Residual: first-party code accepts an operator `error_map` that marks a sub-500 status retryable on a write, and MCP treats a repeated session 404 as not executed; both are operator/protocol assertions under item 9, not a framework inference. INV-006 and INV-021 are clarified; no new invariant, no migration.
Follow-ups: none for F-011 (RESOLVED).

## ADR-025 — Generic inbound channel contract
Status: Accepted
Context: F-012 (origin: Integration Readiness IR-005). `InboundEvent` could be built validly while violating framework correctness; the rules lived in the PostgreSQL inbox, the turn query, the confirmation logic and the RelayPlane mapping. F-013 separately fixed the ghost conversation a duplicate could create.
Decision:
1. Event identity: dedupe is `(tenant_id, channel_id, event_id)`. `event_id` names ONE logical inbound event: a provider redelivery reuses it, different events differ. First writer wins, a duplicate returns `False` and is a state no-op (INV-020, F-013); duplicate content is not compared. A random id per delivery is not a valid identity.
2. `event_id` must be non-empty (model validation, the only behavior change). No generic length limit and no model-level ban of `system:`.
3. The `system:` event-id namespace is the framework's (proactive/internal events, `kind="system"`). Channel adapters must not mint ids there nor emit `kind="system"`; this is an adapter rule, not model validation.
4. Conversation identity: `conversation_id` is scoped by tenant, not channel. It binds `channel_id`, `contact_id` and `scope` for good; a conflicting new event raises `ConversationIdentityConflictError` and is not stored. A channel's id scheme must be deterministic, tenant-unique and channel-namespaced; RelayPlane's `{instance}:{contact}` is its own scheme (INV-074).
5. Trust: `Runtime.receive` is not an authentication boundary. The adapter authenticates its delivery (HMAC is RelayPlane's, not generic) and derives tenant, channel, contact, conversation and scope from authenticated/authoritative context, never from unauthenticated payload fields.
6. RelayPlane (INV-035 reworded to match the code): tenant from the subscription (`relay_tenant_id`, if set, must match); channel from the signed `instance_id`, which must be in `instance_ids` when configured. No RelayPlane behavior changed.
7. Ordering within a conversation: `source_sequence ASC NULLS LAST, occurred_at ASC, received_at ASC, event_id ASC` (INV-075). `source_sequence` is used only when it is trustworthy and meaningfully comparable (monotone, gaps allowed) among events of the same conversation that may be aggregated together; never fabricated from arrival order; sequenced events sort ahead of unsequenced ones, so they are not mixed in one pending stream unless that is intended; if a provider's sequence restarts or changes scope and events of different epochs can coexist, the adapter does not expose the values as comparable; when comparability cannot be guaranteed, `None`. The framework keeps no sequence state or epoch and does not reject mixed input.
8. Clocks: `occurred_at` = best available time the event occurred (fallback order, late events, `last_event_at`, service window; not confirmation evidence; timezone-aware; no skew clamp). `received_at` = the adapter's local receipt time, caller-supplied (`receive` does not stamp it), tie-break and latency only. `provider_occurred_at` = the provider/channel clock of the message, comparable with the same channel's `provider_accepted_at`, else `None`; never derived from `received_at`, local, database or another clock domain (INV-026).
9. Confirmation proof (unchanged algorithm): reply evidence (`reply_to_provider_message_id` naming an ACCEPTED prompt) or provider-clock evidence (ACCEPTED prompt, `provider_accepted_at`, `provider_occurred_at`, same clock domain, outside the tolerance). The tolerance is fixed at 2 s (it is not configurable; the older "configurable per channel" wording was wrong). Missing evidence = ambiguous, nothing executes, the runtime re-prompts. A channel need not support provable confirmation; it then cannot confirm protected actions.
10. `reply_to_provider_message_id`: provider evidence that the event replies to a given outbound provider message; populate when supplied, never invent. `session_id`: required text, stored with the event, the conversation keeps its first value; affects neither dedupe nor order. `kind`: channels emit `user`. `trace_id`: observability only (never identity, dedupe or proof); absent means the turn's own fallback id. `scope`: `receive` stamps the Runtime's when `None`; a conflicting scope is a `ConversationIdentityConflictError`.
11. `Runtime.receive`: `True` new event persisted; `False` duplicate (acknowledge as success); `ConversationIdentityConflictError` (`conversation_agent.core.errors`) permanent conflict, nothing stored; pydantic `ValidationError` when the event is built (adapter bug); any other persistence/infrastructure exception means durable acceptance was NOT confirmed: the adapter does not acknowledge a redeliverable transport and redelivers the SAME logical event with the SAME `event_id`, which is safe through INV-020 (if the earlier attempt committed the retry returns `False`, otherwise the event is stored). No inbound `UNKNOWN` result, no new exception hierarchy, no signature change.
Alternatives: runtime `received_at` stamping, skew clamping, reserving `system:` in the model, rejecting mixed sequences, a configurable tolerance, a channel SDK (all rejected: model/behavior redesign out of scope; the current model can represent every correct event).
Consequences: documentation and public docstrings plus one validation; no migration, no change to dedupe, ordering, confirmation or RelayPlane behavior. A custom adapter must now: reuse `event_id` on redelivery, namespace conversation ids per channel and keep channel/contact/scope fixed per conversation, send `source_sequence=None` unless the channel has a real, comparable sequence (no sequenced/unsequenced mix in one pending stream unless intended, no values from different sequence epochs that can coexist), never copy `received_at` into `provider_occurred_at`, and not send `system` events or `system:` ids as channel input. A correct adapter changes nothing (an empty `event_id` was never a valid identity).
Amended the same day after adversarial review: the `receive` exception wording no longer claims a failed call was definitely not persisted (D11), and the `source_sequence` rule is stated as comparability among events that can be aggregated together rather than a blanket "never resets" (D7). Wording only.
Follow-ups: none for F-012 (RESOLVED).

## ADR-026 — Explicit PostgreSQL schema preflight
Status: Accepted
Context: F-014 (origin: Integration Readiness IR-006). The docs said an ahead or edited schema "prevents startup", but nothing on the startup path (`PostgresDatabase.connect`, `Runtime.build/receive/run/tick`, `app.serve`) verified the schema. The only guard, `Migrator.ensure_current`, was not read-only: it went through `status()`, which ran `CREATE TABLE IF NOT EXISTS schema_migrations` and `ALTER TABLE ... ADD COLUMN IF NOT EXISTS checksum` on every call (so a "check" or `/readyz` probe created metadata on a fresh database and needed DDL privileges). The supported PostgreSQL version was not stated.
Decision:
1. Migration is explicit and mutating: `PostgresDatabase.migrate()` / `Migrator.apply()` / `app.migrate apply` alone bootstrap the metadata, take the advisory migration lock, stamp legacy NULL checksums and apply pending migrations (each in its own transaction). Unchanged, and never invoked implicitly.
2. Schema verification is explicit and read-only: `Migrator.status()` and `Migrator.ensure_current()` create nothing, add no column, stamp nothing, apply nothing and take no lock (no DDL lock, no advisory lock), so it is safe on every start, concurrently, with an ordinary runtime role. A database without the metadata table reports every migration pending (behind) and stays untouched; a metadata table from before checksums is read as it is.
3. The embedding application is the composition root (ADR-022) and calls the preflight before any durable framework work: `connect -> [migrate, when needed] -> ensure_schema_current -> build/start`.
4. `PostgresDatabase.ensure_schema_current()` is the canonical high-level programmatic preflight (returns `None`; raises `SchemaOutOfDateError` for behind or never migrated, `SchemaAheadError`, `MigrationDriftError`; connection failures propagate as the driver raised them). `Migrator.ensure_current()` stays for lower-level users. `app.migrate check`, `/readyz` and `verify_integrity` wrap the same check; no second verification API exists. `verify_integrity` additionally runs the data checks and is not a startup requirement.
5. `Runtime` does not check by itself: `Runtime.build` stays synchronous and I/O-free; `receive`, `run`, `tick` and the workers are unchanged; no verified-database type. One preflight covers every runtime, receive-only host or directly composed worker sharing the database.
6. The documentation no longer claims automatic startup prevention. `apply` does reject drift/ahead states automatically; durable processing requires a successful preflight that the host invokes.
7. PostgreSQL: developed and tested against PostgreSQL 16 (`postgres:16-alpine`). Other versions have not been verified and there is no formal multi-version support matrix. No runtime version check.
8. There is no verified mixed-version rollout contract: the check is exact, so once newer migrations are applied, older code sees the database as ahead and an old instance that re-verifies (e.g. `/readyz`) becomes unready.
Alternatives: verifying inside `Runtime.build` (rejected: it is synchronous and I/O-free, receive-only and direct-composition hosts exist, and it would repeat I/O per runtime); verifying in `run`/`tick` (rejected: `receive` and direct workers bypass it, and `tick` isolates stage failures); a verified-database marker type (rejected: typestate without a need); auto-migrating on start (rejected: mutation must stay an explicit act).
Consequences: no migration and no SQL change. `status`/`check` no longer create `schema_migrations` on a fresh database (explicit `apply` is the only bootstrap), and the verifier no longer needs DDL rights. A host that only migrates during deployment should add the preflight to its startup. Residual observations kept out of this decision: a non-prefix applied history (resolved later by ADR-027) and legacy NULL checksums being trusted by the verifier and stamped by `apply`.
Follow-ups: none for F-014 (RESOLVED).

---

## ADR-027 — Applied migration history is a contiguous prefix
Status: Accepted
Context: F-015 (origin: migration-metadata integrity review after F-014). The migrator classified each known migration independently (row exists -> applied, row missing -> pending), so recorded 0001, 0002, 0004 with 0003 missing looked merely "behind" and an explicit `apply` would run 0003 after 0004. Executed against the shipped set: with 0009 missing and 0010 recorded, `apply` ran 0009 late, dropped and recreated `published_agents`, lost `manifest_digest` and the registry data, and left 0010 recorded as applied.
Decision:
1. Among the migrations this build knows, the recorded versions must be a contiguous prefix of the ordered set (canonical order = sorted file names, never timestamps). A known migration with no row and a later known migration recorded is a `gap`; a missing one with nothing later recorded stays `pending` (ordinary behind, including a fresh database).
2. `MigrationStatus.state` gains `gap` (`applied | pending | changed | unknown | gap`). It is an intentional additive public change; `gap` is not mapped to `pending`, because the distinction is safety-critical.
3. New `MigrationHistoryError(ConversationAgentError)`, a direct sibling of the other migration errors (not a `SchemaOutOfDateError`, which would invite `apply`; no file was necessarily edited; running newer code is no remedy).
4. Precedence is deterministic: changed/drift (`MigrationDriftError`) -> unknown/ahead (`SchemaAheadError`) -> gap (`MigrationHistoryError`) -> pending (`SchemaOutOfDateError`). Unknown/future rows never create a gap by themselves. `status()` reports every state and never raises.
5. `ensure_current`, `apply` and `apply --dry-run` refuse a gap. In `apply` the check precedes the legacy-checksum stamping and every migration: a missing historical migration is never executed after a later recorded one, no row is inserted for it, nothing else runs. The remedy is operator investigation (restore, or reconcile the metadata by hand); the framework does not repair history.
6. Verification stays read-only (ADR-026). NULL-checksum compatibility (trust on first use) is unchanged; the metadata-table name collision stays a separate finding (F-016) and `apply`'s bootstrap is untouched.
Alternatives: treating a gap as `pending` (rejected: the corruption above); subclassing `SchemaOutOfDateError` (rejected: wrong remedy); auto-repairing metadata (rejected: hides corruption, mutates on a verify path).
Consequences: no migration and no SQL change. A correct history and an ordinary prefix-behind database behave as before; a corrupt non-prefix history, previously "repaired" unsafely, now fails closed. Consumers that parse `MigrationStatus.state` must handle `gap`.
Follow-ups: none for F-015 (RESOLVED).

---

## ADR-028 — Tool selection: the model may see fewer tools, never more
Status: Accepted
Context: the tool catalog grows with the number of modules and agents; every tool costs prompt tokens and makes the choice harder. Approved by the project owner on 2026-10-24 (thread "Modelo local no mod-chat").
Decision:
1. A `ToolSelector` port ranks the tools already allowed (capability, policy, allowlist) for the message. The framework keeps only names that are in the allowed list: the output is always a subset (selected ⊆ allowed) and a selector can never add or authorize a tool. Providers in this step: `none` and `rules` (keywords per capability); further providers plug into the same port.
2. Per-agent `tool_selection` in the manifest (absent or `enabled: false` = today's behavior; the digest only carries it when set). With it enabled, at least one of `max_tools_per_call` / `max_tool_tokens` is mandatory and the other may stay off; with both set, whichever is exceeded first triggers the filter. Tokens are estimated (about 4 characters per token of the serialized definition). Also: `top_n`, `min_score`, `always_include`, `on_error: full_list`, `retry_with_full_list`, `max_fallback_rate`.
3. The decision is the journal step `TOOL_SELECTION` (request hash = message + allowed names), so a replay shows the model the same list.
4. Fallbacks: provider error, no candidate above `min_score`, or a disabled filter = the full list. If the model calls an allowed tool the filter hid, that one model call is repeated once with the full list (`retry_with_full_list`). Over `max_fallback_rate` (window of the last 50 triggered selections, at least 20) the filter switches itself off for that runtime (in memory; resets on restart).
5. Metric `tool_selections_total{agent_id,agent_version,outcome}` (applied, skipped, provider_error, fallback, auto_disabled).
Alternatives: filtering by policy only (rejected: no relevance); a provider that can add tools (rejected: breaks allowlist authority).
Consequences: nothing changes for agents without `tool_selection`. A hidden protected tool cannot cause an unconfirmed write: removal only makes the model unable to propose it until the fallback.
Follow-ups: embeddings and classifier (Jev) providers; the model saying it cannot serve the request is not yet treated as a fallback trigger.

---

## ADR-029 — Tool selection provider `http`: an optional external ranking server
Status: Accepted
Context: the owner wants an external server as an optional ranking provider before any native classifier (thread "Adicionar seleção de tools ao framework", 2026-10-24). The server is a new outbound system that receives the user's text.
Decision:
1. New provider `http` of the `ToolSelector` port (ADR-028). It reuses the outbound hardening of tools (`GuardedTransport`, one place): a pre-registered per-tenant connection, HTTPS by default, SSRF and DNS pinning, credential from the `SecretProvider`, no redirects, bounded response.
2. The manifest names only a `connection` id, a `path` and a `timeout_seconds` (capped by the connection). URL, host allowlist, TLS policy and the credential's `secret_ref` stay with the operator in the connection: a manifest never carries a URL or a secret, as for tools (DESIGN §30-31).
3. Versioned wire contract (`docs/TOOL_SELECTOR_CONTRACT.md`, v1): request = message + tool names/descriptions + `top_n`; response = scores in [0, 1]. Names the server invents are ignored (selected ⊆ allowed still holds, INV-077). Any failure raises `ToolSelectorError` and the engine sends the full list (ADR-028).
4. `ToolSelector.rank` gains the keyword-only `tenant_id` (connections and secrets are tenant-scoped). The deployment passes its `GuardedTransport` (`Runtime.build(tool_selection_guard=)`, `tool_selector_for(agent, guard=)`).
5. Only the message and the tools' names/descriptions leave the process; this is documented as personal-data exposure the operator opts into by registering the connection.
Alternatives: URL and secret in the manifest (rejected: breaks the connection/secret rules); sending history or schemas (rejected: data minimization).
Consequences: nothing changes for agents that do not use `http`. A slow server adds up to `timeout_seconds` to a triggered turn before the fallback; keep it small.
Follow-ups: native classifier (Jev) as a provider; a circuit breaker for a down server (today each triggered turn pays the timeout, and `max_fallback_rate` is the only brake).

---

## Questões em aberto (não são decisões)

- A lista de “questões deliberadamente abertas” em [`DESIGN.md` §51](./DESIGN.md) **não foi reauditada** nesta iteration contra o código; alguns itens
  (provider de transcrição, registry de AgentDefinitions, política de migration entre versões, debounce) foram resolvidos por fases posteriores, outros (streaming, memória de longo prazo, sandbox de
  tool Python de terceiros, embeddings) seguem como non-goals. Reauditar é trabalho de uma iteration futura, não um fato aqui afirmado.
- Valores numéricos de produção de lease TTL, heartbeat, `confirmation_attempts`, DeliveryPolicy: dependem de ambiente; não há decisão registrada (a *obrigatoriedade* e a validação da política são a ADR-023).
- Prioridade de Grupos e Visão: **sem decisão** (ver [`04_ROADMAP`](./04_ROADMAP.md)).
