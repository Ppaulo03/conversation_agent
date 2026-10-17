# Runtime Protocol — conversation_agent v4

> **Status: NORMATIVO e vigente** (3º na ordem de autoridade, ADR-020). Visão geral do estado atual: [`02_ARCHITECTURE.md`](./02_ARCHITECTURE.md).

Este documento concentra o protocolo normativo de execução. A visão completa continua em `DESIGN.md`.

## 1. Regra central de ownership

Existem dois fences independentes:

- `conversation_epoch`: protege `ConversationState`, `Turn`, `PendingAction`, `ActionConfirmation`, `TurnJournal` e Outbox gerada pela conversa;
- `execution_epoch`: protege a execução/finalização de uma `ToolInvocation`.

Perder o conversation lease **não apaga o fato externo**. Um executor ainda válido pode finalizar `SUCCEEDED`, `FAILED` ou `UNKNOWN` no ledger usando `execution_epoch`. Ele não pode aplicar esse resultado ao flow/state/outbox sem adquirir o `conversation_epoch` atual.

## 2. Claim de inbound

A ordem é obrigatória:

```text
selecionar conversation candidate sem claim destrutivo
        ↓
acquire conversation lease / conversation_epoch
        ↓
claim ready events somente daquela conversa
        ↓
ordenar + formar burst
```

Se o lease falhar, nenhum evento é claimado pelo worker perdedor.

Eventos duplicados são deduplicados por `(tenant_id, channel_id, event_id)` e nunca criam turno duplicado.

### Contrato do canal de entrada (ADR-025, INV-020/074/075)

`Runtime.receive(InboundEvent)` não é uma fronteira de autenticação: o adapter autentica a entrega do canal e deriva tenant, canal, contato, conversa e scope de contexto autêntico/autoritativo, nunca de campo não autenticado do payload. Resultado: `True` evento lógico novo persistido; `False` duplicata (reconhecer como sucesso, sem estado novo); `ConversationIdentityConflictError` (`conversation_agent.core.errors`) conflito permanente, nada é gravado; `ValidationError` na construção do evento; qualquer outra exceção de persistência/infraestrutura = aceitação durável NÃO confirmada: não reconhecer um transporte que reentrega e reenviar o MESMO evento com o MESMO `event_id` (se a tentativa anterior tiver sido commitada, o dedupe devolve `False`; senão o evento é gravado normalmente, INV-020).

- **`event_id`** identifica UM evento lógico, não vazio; a redelivery reutiliza o mesmo. A identidade de dedupe é `(tenant_id, channel_id, event_id)`; o primeiro a gravar vence, o conteúdo da duplicata não é comparado. O prefixo `system:` é reservado aos eventos do próprio runtime (`kind="system"`); adapters de canal não o usam.
- **`conversation_id`** é único por tenant, determinístico e estável; liga-se a um canal, um contato e um scope e não muda (conflito = `ConversationIdentityConflictError`, evento não gravado). Cada canal escolhe seu esquema, desde que sem colisão entre canais.
- **Ordem** dentro de uma conversa: `source_sequence ASC NULLS LAST, occurred_at ASC, received_at ASC, event_id ASC`. `source_sequence` só é usada quando é confiável e significativamente comparável (crescente; lacunas são aceitas) entre eventos da mesma conversa que podem ser agregados juntos; nunca derivada da ordem local de chegada; eventos com sequência vêm antes dos sem sequência, então não misture os dois num mesmo fluxo pendente salvo intencionalmente; se a sequência do provider reinicia ou muda de escopo e eventos das duas épocas podem coexistir, o adapter não a expõe como comparável. Sem garantia de comparabilidade: `None`.
- **Relógios**: `occurred_at` = quando o evento ocorreu (ordenação de fallback, evento tardio, `last_event_at`, janela de serviço; não é prova de confirmação); `received_at` = recebimento local pelo adapter (desempate e latência; `receive` não o preenche); `provider_occurred_at` = relógio do próprio provider, comparável ao `provider_accepted_at` do mesmo canal, ou `None`.
- **`session_id`** é gravado com o evento, mas a conversa mantém o PRIMEIRO valor; não afeta dedupe nem ordem. **`kind`**: canais emitem `user`; `system` é do runtime. **`trace_id`**: só observabilidade. **`scope`**: se `None`, `receive` usa o do Runtime; depois do primeiro contato, um scope diferente é conflito.

## 3. Lease e heartbeat

O coordinator recebe:

```text
lease_owner
lease_expires_at
conversation_epoch
```

O heartbeat renova o lease por compare-and-set de `lease_owner + conversation_epoch` em intervalo menor que o TTL configurado.

Se o heartbeat falhar:

- o worker fica `stale`;
- não inicia novos steps;
- para na próxima safe boundary;
- I/O externo já em voo termina somente para registrar o resultado no ledger por `execution_epoch`.

Nenhuma transação de banco fica aberta durante LLM ou HTTP externo.

## 4. Side effect protocol

```text
A. PREPARE (conversation_epoch)
   freeze the concrete operation (INV-023): mapped tool args, provider, connection,
     binding fingerprint, recovery contract snapshot — resolved once, with no I/O
   persist logical_step_id / attempt_semantic_id
   create ToolInvocation(PREPARED)
   append journal
   commit

B. EXECUTE (execution_epoch)
   claim invocation -> EXECUTING
   call external provider

C1. FINALIZE FACT (execution_epoch)
   SUCCEEDED / FAILED / UNKNOWN
   persist result/error/provider metadata
   result_application_status = pending
   commit

C2. APPLY TO CONVERSATION (conversation_epoch)
   read terminal ledger result
   update flow/state
   append journal
   create outbox if needed
   result_application_status = applied
   commit
```

Se C1 ocorreu e o worker perdeu o conversation lease, o novo owner aplica C2.

`result_application_status = pending` só é gravado para fatos **terminais** (`SUCCEEDED`, `FAILED`,
`RECONCILED`, `HUMAN_HANDOFF`). Um C1 que resulta em `UNKNOWN` registra status + erro, mas mantém
`result_application_status = none`: o resultado só pode ser aplicado à conversa depois que a
reconciliation o resolver. Enquanto isso o turno permanece aberto (`ToolResultPendingError`) e é
retomado pelo journal; nada é reexecutado.

### Intent congelada, tempo de coordenação e espera (Fase 2.1)

- A `ToolInvocation` guarda `intent`: `tool_args` já mapeados, `provider`, `connection`,
  `binding_fingerprint` (endpoint/risco/idempotência/input_map) e o `recovery` vigente no PREPARE.
  B, retry e reconciliation executam **essa** intent (mesmos args, mesma idempotency key). Se o
  fingerprint resolvido hoje difere, nada é enviado: `INTENT_CHANGED` (FAILED antes do envio; no
  recovery, `HUMAN_HANDOFF`). Mudanças só de output_map/error_map/recovery não movem a operação.
- Lease/claim/backoff são medidos pela *CoordinationTime* (por padrão `clock_timestamp()` do
  PostgreSQL), não pelo relógio de cada worker. O `Clock` da aplicação continua governando o tempo
  conversacional. A UoW exige `lease_expires_at > agora` além de dono+epoch (INV-024).
- `ToolResultPendingError` não é falha: o coordinator libera o lease e devolve `waiting`; apenas
  falhas reais (ex.: LLM) incrementam `turns.attempts`. Um turno que morre depois de um side effect
  emite um aviso determinístico no outbox.
- `provider_metadata` acompanha o resultado até o ledger (nunca ao LLM).

### Destino, lookup e tempo do canal (Fase 3.1)

- **Destino congelado (INV-027).** O PREPARE grava `connection_fingerprint`: hash do destino resolvido
  (scheme, host, porta, path base, política TLS/rede, forma da auth) — **sem segredos**, então rotacionar uma
  credencial não move a operação. Antes de todo envio o provider recalcula e compara; divergência →
  `technical_error DESTINATION_CHANGED` sem enviar (FAILED conhecido na primeira execução; `HUMAN_HANDOFF`
  na reconciliation).
- **Lookup de recovery congelado.** Para `status_lookup`, o PREPARE congela também o lookup (`lookup_intent`:
  args da tool + destino). A reconciliation executa **esse** lookup (`LOOKUP_CHANGED` /
  `LOOKUP_DESTINATION_CHANGED` → handoff): "não encontrado" de uma consulta diferente nunca autoriza reenvio.
- **Resultado do lookup → resultado original.** `RecoverySpec.result_map` converte a saída do lookup na saída
  da capability recuperada; sem `result_map` os schemas precisam ser idênticos (validado ao construir o
  agente). Resultado que não encaixa → `LOOKUP_RESULT_UNUSABLE` (handoff).
- **Tempo do canal (INV-026).** `SendResult.provider_accepted_at` vem do canal ou é `None`; o runtime nunca o
  preenche com horário local. `SendResult` só aceita QUEUED/ACCEPTED/FAILED/UNKNOWN.
- **Catálogo por tenant.** O LLM só enxerga as capabilities que o tenant pode chamar.
- **Retry × effective_risk.** As regras de retry de write valem para qualquer tool *ligada* a uma capability
  protegida, mesmo que a tool se declare `read`.

### Reconciliation (implementação da Fase 2)

- `UNKNOWN`, `EXECUTING` com lease expirado e `RECONCILING` com lease expirado são claimados para
  `RECONCILING` com **novo** `execution_epoch`; o executor/reconciliador anterior fica fenced (C11).
- A estratégia vem do contrato da tool: `status_lookup` (capability de leitura), `retry_same_key`
  (exige `idempotency_supported`), `safe_retry` (somente reads; é o default de um read sem contrato)
  ou `human_handoff` (default de write sem contrato). Só um `business_error` cujo código esteja em
  `absent_codes` prova que a operação não aconteceu e leva a reenvio com a **mesma** idempotency key
  em tool idempotente; qualquer outro erro do lookup deixa a invocation `UNKNOWN`; sem idempotência, handoff.
- O claim é escopado por `agent_id` (o worker serve as definições de um agente).
- Lookup indisponível mantém `UNKNOWN` e agenda um timer durável (`reconcile:<invocation_id>`);
  enquanto o timer existe, a invocation não é reclaimada. Após `max_attempts`, handoff.

## 5. Semantic attempts

`attempt_semantic_id` muda somente em nova tentativa decidida semanticamente pelo usuário ou Flow.

Exemplos que **mudam**:

- usuário: “tenta de novo” após `business_error`;
- flow inicia nova tentativa depois de decisão explícita.

Exemplos que **não mudam**:

- retry técnico seguro;
- replay;
- reconciliation;
- reassignment de worker;
- reenvio com a mesma idempotency key.

Para protected actions, nova tentativa semântica terminal cria nova `PendingAction` e novo `action_id`.

## 6. Turn Journal

Identidade normativa do step:

```text
(turn_id, step_index, step_type)
```

`request_hash` verifica compatibilidade.

Replay:

```text
lookup (turn_id, step_index)
        ↓
step_type igual?
        ↓ não -> JournalDivergenceError
request_hash igual?
        ↓ não -> JournalDivergenceError
        ↓ sim
reproduzir resultado persistido
```

Divergência é fail-closed e gera alerta. Nunca se reexecuta silenciosamente.

`INBOUND_AGGREGATED` persiste `turn_reference_time`. Toda resolução temporal do replay usa esse valor.

### Retenção

Enquanto o turno está aberto, payloads necessários ao replay permanecem disponíveis.

Após estado terminal, inicia `journal_replay_payload_retention` (default inicial: 7 dias, configurável por tenant). Depois disso, conteúdo sensível é reduzido para hashes, metadados e referências permitidas pela política de retenção.

## 7. Protected action confirmation

Ao aceitar uma confirmação, a mesma transaction local grava:

```text
ActionConfirmation
PendingAction -> CONFIRMED
ToolInvocation -> PREPARED
Turn Journal
```

Nunca existe `CONFIRMED` commitado sem `PREPARED`.

A confirmação só pode vir de inbound do contato.

### Evidência do prompt

Um prompt só é elegível em Outbox `ACCEPTED`.

Hierarquia:

1. `reply_to_provider_message_id` correspondente ao prompt;
2. timestamps/ordenação do mesmo domínio do canal;
3. sem prova comparável, tratar como ambígua e re-perguntar.

Com `provider_accepted_at` e `provider_occurred_at`:

```text
occurred < accepted - tolerance -> inelegível
abs(occurred - accepted) <= tolerance -> ambígua
occurred > accepted + tolerance -> elegível
```

A tolerância é fixa em 2 segundos hoje (`Runtime.build` passa `skew_tolerance` ao `ConfirmationStage`); não é configurável por canal. `provider_occurred_at` precisa ser relógio do provider no mesmo domínio de `provider_accepted_at`, ou `NULL`: nunca `received_at`, relógio local ou do banco (INV-026). Sem prova comparável (nem reply-to), a confirmação fica ambígua, nada executa e o runtime re-pergunta.

`SENDING`, `QUEUED` e `UNKNOWN` nunca autorizam protected action.

### Implementação da confirmação (Fase 3)

- **Criação.** Um `REQUIRE_CONFIRMATION` do PolicyGate vira `PendingAction` na **mesma** transação que
  persiste o prompt na Outbox (`outbox.action_id`, `latest_prompt_outbox_id`). `action_id` é determinístico
  (`hash(tenant, conversa, turno, capability, args_hash)`), logo estável no replay. Uma proposta equivalente
  (mesma capability + `args_hash`) reaproveita a ação; uma proposta diferente **invalida** a anterior (INV-010).
  A pergunta de confirmação é do runtime (`ConfirmationTexts.prompt`), não do texto do modelo.
- **Interpretação.** Regras determinísticas e conservadoras primeiro (`sim` confirma; `sim, mas às 16h` é
  `modify`, nunca `confirm`); só então o fallback LLM com saída estruturada, validado pelo runtime
  (confiança mínima para `confirm`; saída malformada = `unclear`). A decisão e a elegibilidade são
  **journaladas** (`CONFIRMATION_DECISION`), então o replay não reavalia contra um estado de storage diferente.
- **Elegibilidade (INV-022).** `reply_to_provider_message_id` do prompt → ordenação do mesmo domínio do canal
  com tolerância de skew (`< accepted - tol` inelegível, dentro de `±tol` ambíguo, `> accepted + tol` elegível)
  → sem evidência comparável = ambíguo. Apenas prompt `ACCEPTED` é elegível. A rajada só é elegível se
  **todas** as mensagens forem.
- **Confirmar.** Uma transação: `ActionConfirmation` + `PendingAction CONFIRMED` + `ToolInvocation PREPARED`
  (`invocation_id = hash(tenant, conversa, action_id)`) + journal. Pontos de falha `C01` (antes), `C02`
  (dentro, rollback total) e `C03` (depois). Em seguida B/C1/C2 pelo ledger. Um turno retomado continua no
  estágio que já journalou, mesmo que a ação já esteja `CONFIRMED`.
- **Não elegível / ambíguo / `unclear`** → re-prompt com **novo** `outbox_id`, `confirmation_attempts+1`
  (limite configurável), prompt `UNKNOWN` anterior vira `SUPERSEDED` (C13). `BEFORE_PROMPT`/`UNRELATED_REPLY`
  não são resposta ao prompt: a mensagem segue como turno normal. Ação expirada (`expires_at` vs
  `turn_reference_time`) → `EXPIRED`.
- **Depois do side effect** o turno nunca falha por causa da composição: se o LLM falhar, a resposta é a
  mensagem determinística `executed_fallback`.

## 8. Outbox / RelayPlane

Mapeamento mínimo:

| RelayPlane | Outbox | Protected confirmation |
|---|---|---|
| `QUEUED` | `QUEUED` | inelegível |
| `ACCEPTED` | `ACCEPTED` | elegível, sujeito à evidência temporal/reply-to |
| `FAILED` | `FAILED` | inelegível |
| `UNKNOWN` | `UNKNOWN` | inelegível |

`ACCEPTED` não significa delivered/read.

Para prompt em `UNKNOWN`, reconciliar. Sem prova de `ACCEPTED`, criar novo prompt com novo `outbox_id`; a tentativa antiga torna-se `SUPERSEDED` para elegibilidade.

A mesma linha de Outbox reutiliza a mesma idempotency key em retry técnico. Re-prompt semântico é nova linha/identidade.

### Política de entrega e semântica genérica (ADR-023, INV-034/072/073)

Normativo e independente de canal. Todo `Runtime` tem **uma** `DeliveryPolicy` explícita (`Runtime.build(delivery_policy=...)` é obrigatório; não existe "sem política", retry ilimitado nem reconciliação desligada). Ela precisa ser segura para todo outbound que o Runtime trata: canais ou tenants com garantias diferentes usam uma política conservadora válida para todos, ou runtimes separados.

```text
safe_resend_window = idempotency_retention - safety_margin
idempotency_retention > 0
0 <= safety_margin < idempotency_retention
0 <= retry_horizon <= safe_resend_window
```

- `idempotency_retention`: quanto tempo o **canal** lembra uma idempotency key. O sender/canal deve honrar essa garantia pelo período em que o Runtime conta com ela.
- `retry_horizon`: limita o retry comum/direto (um `FAILED` retryable provado; a recuperação direta de um `SENDING` obsoleto), medido desde o primeiro envio da linha. **Não** é o último instante em que a entrega pode ser reenviada: `0` desliga todo reenvio; um valor positivo também habilita a recuperação de ambiguidade, e um `UNKNOWN` sem handle pode receber, depois de `retry_horizon`, um reenvio com a mesma chave autorizado pelo reconciliador, até a janela segura fechar.
- `safe_resend_window`: o envelope externo absoluto. Nenhum reenvio sai dele, nem o autorizado pelo reconciliador (a autorização expira em `primeiro_envio + safe_resend_window` e o worker a confere no instante do envio). Consultas de status não são reenvios.
- `retry_horizon == 0` é a representação canônica de "não enviar esta entrega de novo depois da primeira tentativa" (`DeliveryPolicy.no_retry`); não se emula com um `timedelta` minúsculo. O canal de console usa-a (seu dedupe é em memória e não sobrevive a restart).
- Todo reenvio da mesma linha usa a **mesma** `idempotency_key` e o mesmo payload. Chave nova = entrega lógica nova.

| Resultado do sender | Significado | Efeito |
|---|---|---|
| `ACCEPTED` | o canal aceitou | sucesso terminal (não é delivered/read) |
| `QUEUED` | o canal reconheceu o pedido; a liquidação está pendente | não terminal; **carrega** `channel_message_id` (`SendResult` recusa `QUEUED` sem handle); nunca é reenviado às cegas; liquidado por `MessageLookup` **ou** por status empurrado (`apply_channel_status`) |
| `UNKNOWN` | o framework não consegue provar se o efeito externo ocorreu | não terminal. Com handle: liquidar por lookup (se configurado) ou push, sem reenvio cego só porque não há lookup. Sem handle: reenvio com a mesma chave só se `retry_horizon > 0`, o reconciliador autorizar e a janela segura não tiver passado; fora disso continua `UNKNOWN`, observável |
| `FAILED` | o sender/canal **prova** que a entrega não ocorreu | `retryable=false`: terminal. `retryable=true`: reenvio só com a mesma chave, dentro de `retry_horizon`, da janela segura **e** do teto de tentativas da outbox (hoje 5); esgotado qualquer um, a linha fica `FAILED` terminal, nunca `UNKNOWN` |
| exceção de `send(...)` | o framework não sabe se o canal viu o pedido | `UNKNOWN`. Falhas conhecidas antes do envio ou definitivas devem ser devolvidas pelo sender como `FAILED` |
| processo morto/cancelado com a linha em `SENDING` | ambíguo (não é o `FAILED` provado) | reenvio só dentro de `retry_horizon`; fora dele a linha vira/permanece `UNKNOWN`, nunca `FAILED` |

A distinção entre um `FAILED` provado e um `SENDING` obsoleto é preservada sem coluna nova: o claim informa o que a linha era (`OutboundMessage.claimed_from`, não persistido) e `record_result` consome a autorização de reenvio do reconciliador quando um `FAILED` provado volta para `PENDING`.

**Liquidação.** `MessageLookup` é opcional (o `Runtime.build` detecta se o sender o satisfaz, ou recebe `lookup=`). Um canal que pode produzir status não terminal precisa de um caminho viável de liquidação: lookup, push, ou ambos; o framework não consegue prová-lo no build (isso exigiria metadados de capacidade, fora de escopo). Sem lookup, `QUEUED`/`UNKNOWN` com handle ficam não liquidados à espera do push; o sinal é a observabilidade existente de não liquidados. O estado só avança (`apply_channel_status`).

A retenção do RelayPlane e a construção da política a partir de `GET /api/v1/limits` são específicas do adapter: ver [`RELAYPLANE_CONTRACT.md`](./RELAYPLANE_CONTRACT.md).

## 9. Retry de tools

`read`: retry conforme policy.

`write` / `irreversible`: `retry.max_attempts > 1` só é válido se:

```text
idempotency.supported == true
AND retry.mode == same_idempotency_key
```

Timeout/5xx depois de possível envio de write deve virar `UNKNOWN`, salvo garantia explícita do provider de que a operação não ocorreu.

### Contrato genérico do ToolProvider (ADR-024, INV-006/021)

- **Identidade.** `ToolContext.invocation_id` é a identidade lógica da invocação (a coluna `idempotency_key` do ledger é o mesmo valor). A primeira tentativa, todo retry técnico, todo reenvio da reconciliation e toda recuperação após restart/replay recebem o **mesmo** valor. O provider nunca cria uma identidade de dedupe nova por tentativa.
- **Propagação externa.** Em tool não-`read` com `idempotency_supported`, o provider liga `context.invocation_id` ao mecanismo de deduplicação/replay do sistema externo. O mecanismo (header, token, campo nativo, metadado) é do provider; o `Idempotency-Key` HTTP é a escolha dos adapters HTTP/MCP, não um requisito genérico. Se o protocolo não aceita o valor literal, vale uma codificação determinística (mesma identidade externa em toda tentativa, sem aleatoriedade por tentativa).
- **`idempotency_supported`.** Afirmação do operador/provider de que a operação externa realmente deduplica/reproduz a resposta, durante todo reenvio automático que o `RetryPolicy` e o `RecoverySpec` da tool possam fazer (inclusive reenvios da reconciliation, que ocorrem depois de backoff). O framework não consegue provar isso; não há campo de retenção de idempotência de tool.
- **Classificação de write** (risco efetivo diferente de `read`), pelo que se sabe do efeito: efeito provadamente **não** ocorreu → `technical_error` (`retryable=true` só nesse caso: "esta tentativa não produziu o efeito"); recusa de domínio conhecida → `business_error`/`validation_error`/`policy_denied`; requisição pode ter chegado e o efeito não é provável → `UNKNOWN`, sempre (timeout, reset, resposta inutilizável), mesmo com idempotência: idempotência permite à recuperação repetir com segurança, não transforma ambiguidade em prova. Nunca `technical_error`/`timeout`.
- **Retry.** `ToolRunner` só repete `technical_error` com `retryable=true`; em write exige ainda `idempotency_supported` e `same_idempotency_key`. `UNKNOWN` nunca é repetido pelo runner: segue o `RecoverySpec` congelado no PREPARE (`retry_same_key`, `status_lookup`, `human_handoff`).
- **Exceção do provider** é violação de contrato (providers não lançam por falha normal de execução); o runner a classifica de forma defensiva: write → `UNKNOWN`, read → `technical_error` não retryable.
- **Wrappers** (circuit breaker, métricas) preservam a classificação do provider interno (INV-038); só produzem falha própria quando conseguem classificá-la (ex.: circuito aberto, nada enviado).
- **`error_map` do operador** é uma afirmação sujeita ao mesmo contrato: `technical_error` retryable em write exige prova de que o efeito não ocorreu; se o desfecho é ambíguo, o correto é `unknown`. Uma regra arbitrária não é segura por existir.

## 10. Cancelamento

`cancel_requested` é cooperativo.

Nunca abortar semanticamente:

```text
PREPARED -> EXECUTING -> external call -> ledger finalize
```

Parar apenas em safe boundary.

Implementação (Fase 4): a mensagem nova (política `restart`) marca `conversation_states.cancel_requested`; o worker
lê o flag com o heartbeat. A checagem acontece **antes de PREPARE** e antes de cada novo step do LLM/Flow, nunca entre
PREPARED e a finalização. Sem efeito irreversível no turno: turno `CANCELLED` e eventos de volta a `READY`. Com efeito já
executado: o turno termina com aviso determinístico e o efeito é reportado (a mensagem nova roda no turno seguinte).

## 11. Ownership

Primeiro gate do turno:

```text
HUMAN -> persist context; no Router; no LLM; no tool; no automated outbound
HANDOFF_PENDING -> policy específica
BOT -> fluxo normal
```

## 12. Algoritmo resumido do turno

```text
candidate
-> conversation lease
-> claim events for conversation
-> burst
-> turn + journal + turn_reference_time
-> ownership gate
-> confirmation / active flow / router
-> capability
-> policy
-> PREPARE
-> external execution
-> ledger finalize by execution_epoch
-> apply result by conversation_epoch
-> compose
-> outbox
-> consume inbox
-> release lease
```

## 13. Chaos cases

A tabela completa (`C01`–`C16`, fase de exigência e resultado esperado) está em [`ROADMAP.md`](./ROADMAP.md#chaos-gates). Os casos que exercitam este protocolo diretamente:

| Parte do protocolo | Casos |
|---|---|
| §4 A (PREPARE) / §7 (confirmação atômica) | `C01`, `C02`, `C03`, `C04` |
| §4 B (EXECUTE) | `C04`, `C05`, `C12` |
| §4 C1 (finalize do fato, `execution_epoch`) | `C06`, `C14` |
| §4 C2 (aplicar à conversa, `conversation_epoch`) | `C07`, `C14`, `C16` |
| §1 / §3 (fences, lease, heartbeat) | `C09`, `C14` |
| §6 (journal, replay fail-closed) | `C10`, `C15`, `C16` |
| §7 / §8 (prompt de confirmação e Outbox) | `C08`, `C13` |
| reconciliação de `UNKNOWN` | `C05`, `C11` |
