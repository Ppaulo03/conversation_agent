# Runtime Protocol — conversation_agent v4

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

`confirmation_clock_skew_tolerance` é configurável por canal.

`SENDING`, `QUEUED` e `UNKNOWN` nunca autorizam protected action.

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

### Retenção da idempotency key no RelayPlane

A arquitetura anterior do RelayPlane não define uma janela temporal outbound formal. Produção deve configurar/documentar `relayplane_idempotency_retention` e garantir:

```text
sender_retry_horizon <= relayplane_idempotency_retention
```

Depois dessa janela, não é permitido reenviar cegamente contando com dedupe do RelayPlane; deve haver reconciliação/status lookup ou escalonamento.

## 9. Retry de tools

`read`: retry conforme policy.

`write` / `irreversible`: `retry.max_attempts > 1` só é válido se:

```text
idempotency.supported == true
AND retry.mode == same_idempotency_key
```

Timeout/5xx depois de possível envio de write deve virar `UNKNOWN`, salvo garantia explícita do provider de que a operação não ocorreu.

## 10. Cancelamento

`cancel_requested` é cooperativo.

Nunca abortar semanticamente:

```text
PREPARED -> EXECUTING -> external call -> ledger finalize
```

Parar apenas em safe boundary.

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
