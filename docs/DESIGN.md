# conversation_agent — Design mestre v4

> **Status: racional de desenho (parcialmente histórico).** Não é a descrição do estado atual — para isso use [`02_ARCHITECTURE.md`](./02_ARCHITECTURE.md);
> decisões vigentes estão em [`03_DECISIONS.md`](./03_DECISIONS.md). Este arquivo não foi auditado linha a linha contra o código (finding F-004 em [`05_REVIEW_LOG.md`](./05_REVIEW_LOG.md)).
> Precedência: ver ADR-020. As afirmações “fonte única” abaixo valem apenas no sentido de ADR-020.

Framework para construção e execução de **agentes conversacionais stateful**, com LLM, extração estruturada, flows, tool calling, policies, integrações externas e mensageria assíncrona.

**Status:** documento mestre v4 para início da implementação. Decisões marcadas como normativas devem ser tratadas como invariantes do runtime; questões explicitamente abertas podem evoluir com evidência da implementação.

A versão v4 fecha as fronteiras de claim/lease, separa `conversation_epoch` de `execution_epoch`, torna o replay do journal fail-closed, formaliza a evidência temporal de confirmação e detalha o contrato de entrega com o RelayPlane.

Para manutenção, as partes normativas também são publicadas separadamente em:

- `docs/INVARIANTS.md`;
- `docs/RUNTIME_PROTOCOL.md`;
- `docs/ROADMAP.md`.

Este arquivo (`DESIGN.md`) é a visão consolidada; em caso de divergência, prevalecem `INVARIANTS.md`, `RUNTIME_PROTOCOL.md` e `ROADMAP.md` (ver §52).

O framework é projetado para operar como **orquestrador conversacional**. Ele não implementa o domínio de negócio do cliente e não assume que reservas, pedidos, tickets, pagamentos, estoque ou agenda vivem dentro dele.

O caso de uso inicial que guia a implementação é um agente de **agendamento de serviços** conectado a uma API externa de referência. Esse caso serve para provar o runtime, não para tornar scheduling uma dependência do framework.

---

# 1. Regras de ouro

## 1.1. Estado conversacional ≠ estado de negócio

> **Conversation Agent owns conversational state, not business state.**

O runtime é autoridade sobre:

- conversa e sessão atuais;
- turno atual;
- flow ativo;
- slots coletados e validados;
- ação pendente de confirmação;
- confirmação ou rejeição explícita do usuário;
- referências a invocações de tools;
- ownership BOT/HUMAN;
- mensagens de saída produzidas pelo turno;
- timers conversacionais;
- memória conversacional opcional.

O runtime **não é autoridade** sobre:

- reservas;
- pedidos;
- estoque;
- pagamentos;
- clientes de negócio;
- tickets;
- agendas;
- ordens de serviço;
- contratos;
- qualquer outra entidade cujo source of truth esteja em outro sistema.

Esses estados pertencem às APIs e sistemas externos acessados por tools.

---

## 1.2. O framework orquestra; tools integram

O framework conhece:

- schemas de entrada e saída;
- capabilities semânticas;
- bindings entre capability e tool;
- policies;
- idempotência e lifecycle de invocação;
- timeout, retry e reconciliação;
- contexto confiável;
- providers de execução.

O framework **não precisa conhecer a implementação de negócio da tool**.

Uma tool pode chamar:

- HTTP;
- MCP;
- gRPC;
- função Python;
- ERP;
- CRM;
- serviço interno;
- sistema de agenda;
- qualquer backend compatível.

---

## 1.3. Packs são opcionais e posteriores à prova do comportamento

Um agente pode ser construído sem Pack.

Um Pack é apenas uma unidade reutilizável de distribuição de definições:

- intents;
- flow definitions;
- capability requirements;
- policies;
- prompt fragments;
- schemas;
- evals.

O runtime não depende de Pack durante a execução.

> **Pack is an optional distribution unit for reusable agent behavior.**

## 1.4. Invariantes arquiteturais

As invariantes normativas (`INV-001` em diante; o conjunto atual vai até `INV-071`), com o teste obrigatório de cada uma, vivem em [`INVARIANTS.md`](./INVARIANTS.md), que é a **fonte única**. Este documento descreve o desenho que as sustenta e não as repete.

Resumo do que elas garantem: `core`/`engine` não conhecem adapters; o LLM nunca fornece identidade, secrets nem autoridade; toda operação externa cruza `Capability -> Binding -> PolicyGate -> ToolRunner`; ação protegida tem `action_id` persistente e confirmação restrita a ela; `UNKNOWN` nunca sofre retry cego; mutação de conversa e execução externa têm fences independentes; replay do journal é fail-closed; `HUMAN` nunca recebe resposta automática; e evento duplicado nunca cria turno duplicado.

O primeiro agente real deve ser implementado **sem Pack obrigatório**. O primeiro Pack só é extraído depois que existir comportamento reutilizável comprovado.

---

## 1.5. O LLM não é autoridade

O LLM pode:

- interpretar linguagem;
- propor intents;
- propor valores para slots;
- escolher capabilities permitidas;
- produzir argumentos estruturados;
- responder em linguagem natural;
- lidar com digressões e conversa aberta.

O LLM não decide:

- tenant;
- identidade do contato;
- permissões;
- isolamento multi-tenant;
- risk efetivo;
- se uma ação protegida pode executar;
- se o usuário confirmou uma ação diferente daquela apresentada;
- estado real de sistemas externos;
- idempotência;
- ordering de eventos;
- datas relativas sem passar por normalização determinística.

---

# 2. Stack e decisões base

| Tema | Decisão |
|---|---|
| Linguagem | Python 3.12 |
| Concorrência | `asyncio` |
| Modelos/validação | Pydantic v2 |
| HTTP/webhook | FastAPI |
| Dependências | `uv` |
| Qualidade | `ruff`, `pytest`, `import-linter` |
| Arquitetura | Hexagonal / Ports & Adapters |
| Agent loop | próprio, pequeno e controlado |
| Framework LLM | sem dependência obrigatória de LangChain/LlamaIndex |
| Persistência inicial | PostgreSQL |
| Observabilidade | OpenTelemetry |
| Canal inicial | RelayPlane |
| NLU inicial | regras + extração estruturada assistida por LLM + validação determinística |
| Embeddings para intent | **não obrigatórios na v1**; entram somente se métricas de custo/latência justificarem |

---

# 3. Modelo conceitual

A unidade de configuração é o **AgentDefinition**.

```text
AgentDefinition
 ├── Persona
 ├── Intents
 ├── Flows
 ├── Capability Definitions / Requirements
 ├── Capability Bindings
 ├── Tool Definitions
 ├── Policies
 ├── Prompt Fragments
 ├── Evals
 └── Optional Packs
```

O runtime executa uma forma resolvida e imutável:

```text
AgentDefinition
      +
Optional Packs
      +
Tool Catalog
      +
Connections
      ↓
 AgentCompiler
      ↓
 CompiledAgent
```

O `AgentCompiler` é importante, mas **não é pré-requisito para o primeiro vertical slice**. A primeira implementação pode montar esses objetos diretamente em Python; YAML/manifestos entram depois como serialização de um modelo já provado.

---

# 4. Capabilities

Uma **Capability** expressa o que o agente precisa fazer, sem dizer como.

Exemplos:

```text
scheduling.availability
scheduling.create
scheduling.cancel
knowledge.search
customer.lookup
order.lookup
ticket.create
inventory.check
```

Exemplo:

```yaml
capabilities:
  scheduling.create:
    description: Cria um agendamento.

    input:
      service_id: string
      start_at: datetime
      duration_minutes: integer

    output:
      booking_id: string
      status: string

    risk: irreversible
    confirmation_required: true
```

Uma Capability é um **contrato semântico estável** consumido por Flow/LLM.

---

# 5. Tools

## 5.1. Tool Definition

Uma Tool Definition descreve uma operação concreta disponível ao agente.

```yaml
tools:
  erp_create_reservation:
    description: Cria uma reserva no ERP.

    input_schema:
      service_code: string
      starts_at: datetime
      hours: decimal

    output_schema:
      id: string
      state: string

    risk: irreversible
    confirmation_required: true
    timeout: 5s

    retry:
      max_attempts: 2
      mode: same_idempotency_key

    idempotency:
      supported: true
```

Risk classes iniciais:

```text
read < write < irreversible
```

---

## 5.2. Effective risk e confirmação

Capability e Tool podem declarar risk/confirmation. O binding **nunca pode reduzir proteção**.

Regra:

```text
effective_risk = max(capability.risk, tool.risk, binding.risk_if_any)

effective_confirmation =
    capability.confirmation_required
 OR tool.confirmation_required
 OR binding.confirmation_required
```

O `AgentCompiler` rejeita qualquer configuração que tente rebaixar o nível de risco ou remover confirmação exigida por uma das pontas.

Para `write` e `irreversible`, o compiler também rejeita `retry.max_attempts > 1` quando `idempotency.supported != true` ou quando a policy de retry não reutiliza explicitamente a mesma idempotency key. Retry técnico nunca cria uma nova tentativa semântica.

Policies adicionais podem elevar proteção em runtime.

---

# 6. Capability Binding

O binding liga um contrato semântico a uma Tool Definition concreta.

```yaml
bindings:
  scheduling.create:
    tool: erp_create_reservation
```

Para que a mesma Capability funcione sobre APIs diferentes, o binding possui adaptação explícita de entrada e saída.

## 6.1. Input mapping

```yaml
bindings:
  scheduling.create:
    tool: erp_create_reservation

    input_map:
      service_code: $.service_id
      starts_at: $.start_at
      hours:
        from: $.duration_minutes
        transform: minutes_to_hours
```

## 6.2. Output mapping

```yaml
    output_map:
      booking_id: $.id
      status: $.state
```

Mappings de saída também suportam listas e envelopes paginados de forma explícita. Exemplo:

```yaml
    output_map:
      slots:
        from_each: $.items[*]
        map:
          start_at: $.start
          end_at: $.end
      next_cursor: $.pagination.next_cursor
```

O Flow e o LLM sempre enxergam o schema da **Capability**, nunca o schema específico da API externa.

## 6.3. Error mapping

O binding define como resultados do provider são convertidos para a taxonomia canônica de erros do runtime.

```yaml
bindings:
  scheduling.create:
    tool: erp_create_reservation

    error_map:
      http_status:
        400: {type: validation_error}
        409: {type: business_error, code: SLOT_UNAVAILABLE}
        429: {type: technical_error, retryable: true}

      default_5xx:
        read: {type: technical_error, retryable: true}
        write: {type: unknown}

      timeout:
        read: {type: technical_error, retryable: true}
        write: {type: unknown}
```

Regra conservadora:

> `technical_error` só pode ser usado quando o runtime sabe que o side effect não ocorreu ou quando repetir a operação é seguro pelo contrato. Se uma escrita pode ter alcançado o sistema externo e o resultado ficou incerto, o resultado é `unknown`.

`error_map` pode considerar HTTP status, códigos de erro do body, tipo de operação e garantias do provider, mas nunca pode reduzir uma situação ambígua para retry cego.

## 6.4. Linguagem de mapping

A v1 deve começar com operações restritas e verificáveis:

- rename/select;
- constant;
- enum mapping;
- conversão de unidade;
- normalização/formatação de datetime;
- composição simples de campos.

YAML **não executa Python arbitrário**.

Quando o mapping declarativo não for suficiente, pode existir um `ArgumentMapper`/`ResultMapper` programático registrado pelo composition root.

O compiler valida, sempre que possível:

```text
Capability input -> input_map -> Tool input
Tool output      -> output_map -> Capability output
```

---

# 7. Tool Providers

Provider é responsável somente pela execução técnica.

```python
class ToolProvider(Protocol):
    async def execute(
        self,
        binding: ResolvedToolBinding,
        args: dict,
        context: ToolContext,
    ) -> ToolResult: ...
```

Providers previstos:

```text
HTTPToolProvider
MCPToolProvider
PythonToolProvider
FakeToolProvider
ReplayToolProvider
```

Posteriormente:

```text
OpenAPIToolProvider
GRPCToolProvider
```

---

# 8. Tool Context

Contexto confiável é injetado pelo runtime e nunca preenchido pelo modelo.

```python
class ToolContext(BaseModel):
    tenant_id: str
    agent_id: str
    agent_version: str
    channel_id: str
    conversation_id: str
    session_id: str
    contact_id: str
    turn_id: str
    invocation_id: str
    trace_id: str
```

O LLM fornece apenas argumentos de negócio autorizados pelo schema.

---

# 9. Idempotência correta

## 9.1. `tool_call_id` não define identidade de ação protegida

`tool_call_id` gerado por LLM é efêmero. Após crash/replay, o LLM pode gerar outro identificador.

Portanto, para ações protegidas/confirmadas:

```text
invocation_id = hash(
    tenant_id,
    conversation_id,
    action_id
)
```

A `action_id` pertence à `PendingAction` confirmada e permanece estável entre retries/reexecuções.

Para operações não protegidas, a estratégia pode usar uma chave estável produzida pelo runtime, por exemplo:

```text
hash(
    tenant_id,
    conversation_id,
    turn_id,
    logical_step_id,
    canonical_args
)
```

A identidade de uma side effect **nunca depende somente de um identificador produzido pelo LLM**.

---

## 9.2. Canonicalização de argumentos

`args_hash` é calculado **após**:

1. validação pelo schema da Capability;
2. aplicação de defaults;
3. normalização de datas/timezones;
4. normalização de números/decimais/unidades;
5. ordenação determinística das chaves;
6. serialização canônica;
7. hash criptográfico.

Por padrão, **todos os argumentos da Capability são protegidos**.

Campos podem ser explicitamente declarados como não protegidos apenas quando sua alteração não muda o significado da ação.

`ToolContext`, secrets e metadados de transporte não entram no `args_hash`.

---

# 10. Confirmação de ações — máquina de estados

Confirmação é ligada a uma ação específica e auditável.

```python
class PendingAction(BaseModel):
    action_id: str
    capability: str
    tool_name: str
    canonical_args: dict
    args_hash: str
    protected_fields: list[str]
    summary: str
    created_from_turn: str
    latest_prompt_outbox_id: str | None
    confirmation_attempts: int
    expires_at: datetime | None


class ActionConfirmation(BaseModel):
    action_id: str
    inbound_message_id: str
    prompt_outbox_id: str
    reply_to_provider_message_id: str | None
    decision: Literal["confirm", "reject", "modify", "unclear"]
    interpreter: Literal["rule", "llm"]
    confidence: float | None
    occurred_at: datetime
    confirmed_at: datetime | None
```

Lifecycle:

```text
NONE
  ↓ policy requires confirmation
PENDING_CONFIRMATION
  ├── confirm  -> CONFIRMED -> PREPARED atomically -> execute
  ├── reject   -> REJECTED
  ├── modify   -> INVALIDATED -> update flow -> create new PendingAction
  ├── unclear  -> PENDING_CONFIRMATION / re-prompt
  └── timeout  -> EXPIRED
```

A resposta do usuário é interpretada por um **ConfirmationInterpreter** restrito:

```text
confirm
reject
modify
unclear
```

Estratégia inicial:

1. regras determinísticas para respostas simples (`sim`, `não`, `confirmo`, etc.);
2. fallback LLM com structured output estrito;
3. validação pelo runtime.

Uma confirmação só é válida quando:

- vem de uma mensagem **inbound do contato** da conversa;
- referencia uma `PendingAction` ainda válida;
- `prompt_outbox_id` corresponde a um prompt de confirmação em estado `ACCEPTED`;
- existe evidência de que a resposta pertence a um prompt elegível;
- `args_hash` continua igual;
- a ação não expirou;
- ownership da conversa permite execução automática.

A evidência é avaliada nesta ordem:

1. **reply-to do canal**, quando disponível: `reply_to_provider_message_id` igual ao `provider_message_id` do prompt é a evidência primária;
2. **ordenação/timestamp do mesmo domínio do canal**, quando o RelayPlane fornecer `provider_accepted_at` e `provider_occurred_at` comparáveis;
3. sem reply-to nem relógio comparável, a confirmação é considerada temporalmente ambígua e o runtime re-pergunta; `received_at` local sozinho não prova que o usuário respondeu ao prompt correto.

Para comparação temporal pelo provider, existe `confirmation_clock_skew_tolerance`, configurável por canal. Regra padrão da v1:

```text
provider_occurred_at < provider_accepted_at - tolerance -> resposta inelegível
|provider_occurred_at - provider_accepted_at| <= tolerance -> ambígua; re-prompt
provider_occurred_at > provider_accepted_at + tolerance -> elegível
```

Reply-to explícito para o prompt correto pode resolver a zona ambígua. Saída de tool, mensagem de sistema, resposta do próprio agente ou um `sim` anterior ao prompt nunca confirmam uma ação.

Exemplo:

```text
Assistente: Confirma amanhã às 15h por 2 horas?
Usuário: Sim, mas às 16h.
```

Resultado:

```text
modify
```

A ação anterior é invalidada. O slot é atualizado e uma **nova** `PendingAction` é criada.

## 10.1. Confirmação e prepare são atômicos

Ao aceitar `confirm`, a mesma `ConversationUnitOfWork` deve:

```text
insert ActionConfirmation
PendingAction -> CONFIRMED
create ToolInvocation(status=PREPARED, same action_id)
append journal entries
COMMIT
```

Ou todas essas mudanças existem, ou nenhuma existe. Não pode haver `PendingAction=CONFIRMED` commitada sem uma invocação `PREPARED` correspondente.

O recovery também procura, por defesa, qualquer inconsistência histórica `CONFIRMED sem PREPARED` e a envia para reconciliação/handoff; porém a implementação normal não deve produzi-la.

## 10.2. Re-perguntas e expiração

`confirmation_attempts` possui limite configurável. Ao exceder o limite, o runtime expira a ação ou solicita handoff conforme policy.

Cada re-prompt gera novo `outbox_id`. A Outbox guarda `action_id`, e `PendingAction.latest_prompt_outbox_id` aponta para a tentativa mais recente. Reply-to explícito pode referenciar qualquer prompt `ACCEPTED` ainda associado à mesma `action_id`; sem reply-to, somente a tentativa elegível mais recente participa da decisão temporal.

# 11. Policy Gate

Toda operação externa passa pelo mesmo gate.

```text
Flow / LLM
    ↓
Capability Request
    ↓
Resolved Binding
    ↓
PolicyGate
    ├── ALLOW
    ├── DENY
    └── REQUIRE_CONFIRMATION
```

Policies cobrem:

- confirmação explícita;
- allowlist de capability/tool;
- restrições por tenant;
- limites monetários;
- horários permitidos;
- número máximo de tentativas;
- human handoff;
- regras específicas da aplicação.

O prompt nunca é a única defesa de uma invariante.

## 11.1. Guardrails operacionais

Além das policies de negócio, o runtime aplica guardrails independentes do domínio:

- conteúdo do usuário, documentos recuperados e resultados de tools são **dados não confiáveis**, nunca instruções com autoridade superior;
- allowlist de capabilities/tools por agente e tenant;
- rate limit por contato, tenant e conexão externa;
- limite de steps, tool calls e wall-clock por turno;
- orçamento de tokens e custo por turno/sessão/tenant;
- detecção de loop por repetição de capability + args/result;
- tamanho máximo de prompts, resultados de tools e payloads persistidos;
- truncamento/sumarização controlada de resultados extensos;
- PII/secrets redaction em logs e traces;
- limite de falhas consecutivas antes de handoff;
- circuit breakers e backpressure nos adapters externos.

Nenhum texto retornado por uma tool pode alterar policies, tenant context, bindings ou permissões em runtime.

---

# 12. Tool Invocation Ledger

Toda invocação recebe lifecycle persistente.

```text
ToolInvocation

invocation_id
tenant_id
agent_id
conversation_id
session_id
turn_id
step_index
logical_step_id
attempt_semantic_id
action_id?              # obrigatório para ação protegida
tool_name
capability
args_hash
idempotency_key

status:
  prepared
  executing
  succeeded
  failed
  unknown
  reconciling
  reconciled

execution_owner?
execution_lease_expires_at?
execution_epoch
result_application_status   # pending | applied
result_applied_at?
result
error
provider_metadata
prepared_at
started_at
finished_at
```

Regras:

- `FAILED` terminal nunca retorna a `EXECUTING`;
- nova tentativa semântica de uma ação protegida cria nova `PendingAction` e novo `action_id`;
- retomada técnica da mesma ação usa o mesmo `invocation_id`/idempotency key;
- `execution_epoch` impede que um executor obsoleto finalize uma invocação assumida por outro worker;
- `logical_step_id` é criado pelo runtime e nunca depende apenas de identificador emitido pelo LLM;
- o fato `SUCCEEDED/FAILED/UNKNOWN + result/error` é finalizado por compare-and-set de `execution_epoch`, mesmo que o worker tenha perdido o `conversation_epoch`;
- aplicar esse fato ao Flow/ConversationState/Outbox exige adquirir o `conversation_epoch` atual;
- `result_application_status=pending` permite que o novo dono da conversa aplique um resultado externo já persistido.

---

# 13. `unknown` é um estado de primeira classe

Timeout ou crash depois de enviar uma chamada externa podem deixar o resultado ambíguo.

Exemplo:

```text
POST /bookings
   ↓
API criou reserva
   ↓
resposta se perdeu
   ↓
runtime vê timeout
```

O runtime **não pode simplesmente repetir** a ação.

A Tool Definition declara uma estratégia de recovery:

```yaml
idempotency:
  supported: true

recovery:
  strategy: retry_same_key
```

ou:

```yaml
recovery:
  strategy: status_lookup
  capability: scheduling.lookup_by_idempotency_key
```

ou:

```yaml
recovery:
  strategy: human_handoff
```

Estratégias iniciais:

### A. `retry_same_key`

Para sistemas que garantem idempotência por chave.

### B. `status_lookup`

Consulta o sistema externo usando:

- idempotency key;
- external request id;
- business reference;
- capability específica de status.

### C. `human_handoff`

Para operação irreversível sem idempotência nem forma confiável de consulta.

Regra conservadora:

> **Ação protegida em estado `unknown` não é executada novamente sem reconciliação.**

---

# 14. Recuperação após crash

Ao iniciar/reassumir processamento, o runtime procura invocações abandonadas em:

```text
prepared
executing
unknown
reconciling
```

Também valida por defesa:

```text
PendingAction CONFIRMED sem ToolInvocation PREPARED
```

A implementação normal impede esse estado pela transação atômica do §10.1; encontrá-lo indica dado legado, migração ou corrupção parcial e exige reparo/reconciliação, nunca execução cega.

Uma invocação em `executing` cujo lease expirou não é presumida como falha. Um novo worker só a assume após adquirir novo `execution_epoch`.

Ela entra no processo de reconciliação configurado pela Tool Definition.

Isso cobre o crash entre:

```text
external system accepted request
              ↓
local runtime persisted succeeded
```

---

# 15. Atomicidade local e Unit of Work

Não existe transação distribuída entre Postgres e APIs arbitrárias. O design não promete isso.

O que o runtime garante é **atomicidade das mutações locais relacionadas ao turno**.

Port principal:

```python
class ConversationUnitOfWork(Protocol):
    state: ConversationStateRepository
    turns: TurnRepository
    journal: TurnJournalRepository
    confirmations: ConfirmationRepository
    invocations: ToolInvocationRepository
    outbox: OutboxRepository

    conversation_epoch: int

    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
```

Todo `commit()` que modifica **estado conversacional** valida o `conversation_epoch`/fencing token adquirido pelo Turn Coordinator. Se outro worker já assumiu a conversa, o commit falha com conflito de concorrência.

O ledger possui uma fronteira separada para registrar o fato externo após I/O:

```python
class ToolInvocationStore(Protocol):
    async def claim_execution(self, invocation_id: str, owner: str) -> int: ...
    async def finalize_execution(
        self,
        invocation_id: str,
        execution_epoch: int,
        result: ToolResult,
    ) -> None: ...
```

`finalize_execution` valida somente `execution_epoch`; ele **não exige** o `conversation_epoch`. Isso permite registrar o resultado real de uma chamada já iniciada mesmo que o worker tenha perdido ownership da conversa. Depois, a aplicação desse resultado ao state/flow/outbox ocorre em outra UoW cercada pelo `conversation_epoch` atual.

Uso conceitual:

```python
async with uow_factory.begin(tenant_id, conversation_id) as uow:
    ... mutate conversation state ...
    ... write ledger transition ...
    ... append outbound rows ...
    await uow.commit()
```

Estado conversacional, transições do ledger e linhas do outbox produzidas pela mesma decisão ficam na **mesma transação PostgreSQL**.

---

# 16. Protocolo de execução de uma side effect

Uma chamada externa possui duas fronteiras locais de commit.

## Passo A — prepare

Transação local:

```text
validate ownership + conversation_epoch
validate state
validate policy/confirmation
create/lock invocation
status = prepared
persist relevant conversation state + journal
COMMIT
```

Para ação protegida confirmada por uma mensagem inbound, `ActionConfirmation + PendingAction(CONFIRMED) + ToolInvocation(PREPARED)` são criados **na mesma transação**.

Depois do commit, o executor pode adquirir a invocation (`PREPARED -> EXECUTING`) usando `execution_owner + execution_epoch`, sem manter a transação da conversa aberta durante I/O externo.

## Passo B — external call

```text
ToolProvider.execute(..., same idempotency key)
```

## Passo C1 — finalize o fato externo

Transação curta do ledger, cercada por `execution_epoch`:

```text
validate execution_epoch
ledger -> succeeded / failed / unknown
persist result/error/provider metadata
result_application_status = pending
COMMIT
```

Essa gravação independe do `conversation_epoch`.

## Passo C2 — aplique o resultado à conversa

Depois de obter o lease/`conversation_epoch` atual da conversa:

```text
read terminal ledger result
update flow/state
append journal
create outbox messages when appropriate
mark result_application_status = applied
COMMIT with conversation_epoch fencing
```

Se o worker que realizou C1 perdeu a conversa, ele não executa C2; o novo owner/reconciliation worker encontra o resultado `pending` e o aplica.

A janela entre B e C1 é inevitável. Ela é tratada por **idempotência + reconciliation**, não por uma falsa promessa de atomicidade distribuída.

---

# 17. Durable Inbox

Inbound é persistido antes do processamento.

```text
RelayPlane
    ↓
Webhook Adapter
    ↓
verify signature
    ↓
normalize envelope
    ↓
InboxStore.insert_if_absent(event_id)
    ↓
HTTP 2xx
```

Port:

```python
class InboxStore(Protocol):
    async def insert_if_absent(self, event: InboundEvent) -> bool: ...
    async def list_ready_conversations(self, ...) -> list[ConversationKey]: ...
    async def claim_ready_for_conversation(
        self,
        conversation: ConversationKey,
        owner: str,
        ...
    ) -> list[InboundEvent]: ...
    async def mark_consumed(self, event_ids: list[str]) -> None: ...
```

`list_ready_conversations` é somente seleção de candidatos; não muda ownership dos eventos. O Turn Coordinator primeiro adquire o lease da conversa e **só depois** faz `claim_ready_for_conversation`. Assim dois workers não formam bursts concorrentes da mesma conversa. Se o lease não for adquirido, nenhum evento daquela conversa é claimado por esse worker.

Estrutura conceitual:

```text
inbound_events

id
tenant_id
event_id
channel_id
conversation_id
source_sequence?
occurred_at
received_at
payload
status
```

---

# 18. Ordering e eventos tardios

Entrega at-least-once pode chegar duplicada ou fora de ordem.

Ordenação preferencial:

1. `source_sequence`, quando o canal fornecer uma sequência confiável;
2. `occurred_at` normalizado;
3. `received_at` + `event_id` como desempate estável.

O `BurstAggregator` usa um watermark/janela de silêncio.

Depois que um turno foi finalizado:

> **um evento tardio nunca reescreve retroativamente o turno já commitado.**

Ele entra no próximo turno com metadata indicando que chegou atrasado.

Isso preserva replay determinístico.

---

# 19. Durable Scheduler

Debounce, expiração e mensagens proativas exigem timers que sobrevivam a restart.

Port:

```python
class Scheduler(Protocol):
    async def schedule(self, key: str, due_at: datetime, event: ScheduledEvent) -> None: ...
    async def cancel(self, key: str) -> None: ...
```

Casos de uso:

- flush de debounce;
- expiração de PendingAction;
- timeout de handoff;
- retry/reconciliation;
- lembretes proativos;
- jobs conversacionais programados.

O backend inicial pode usar PostgreSQL + worker de timers.

---

# 20. Turn Coordinator

Cada conversa/sessão é processada como um ator serializado, mas **nenhuma transação de banco permanece aberta durante chamada ao LLM ou a sistema externo**.

```text
conversation A -> um turno por vez
conversation B -> pode executar em paralelo
```

## 20.1. Lease + fencing

Ao adquirir uma conversa, o coordinator recebe:

```text
lease_owner
lease_expires_at
conversation_epoch   # monotônico / fencing token
```

Cada reassignment incrementa `conversation_epoch`. Toda mutação de:

- ConversationState;
- Turn;
- PendingAction/Confirmation;
- TurnJournal;
- criação de `ToolInvocation(PREPARED)` enquanto parte da decisão conversacional;
- Outbox produzida pelo turno;

exige o epoch atual.

Depois que uma invocação foi claimada para execução, as transições `EXECUTING -> SUCCEEDED|FAILED|UNKNOWN` pertencem ao ledger e são protegidas por `execution_epoch`, não por `conversation_epoch`. Aplicar o resultado do ledger de volta à conversa volta a exigir `conversation_epoch`.

Exemplo de proteção conceitual:

```sql
UPDATE conversation_state
SET ..., version = version + 1
WHERE tenant_id = :tenant
  AND conversation_id = :conversation
  AND version = :expected_version
  AND conversation_epoch = :expected_epoch;
```

Se `rowcount = 0`, o worker perdeu ownership e não pode realizar commit.

Um worker zumbi pode terminar uma chamada HTTP que já estava em voo. Se ainda possuir o `execution_epoch` da invocação, ele pode persistir o fato externo no ledger, mas não pode alterar ConversationState/Flow/Outbox sem o `conversation_epoch` atual. O novo owner aplica o resultado via ledger/reconciliation.

O lease da conversa possui heartbeat durante operações longas de LLM/tool. A renovação é um compare-and-set por `lease_owner + conversation_epoch` e ocorre em intervalo menor que o TTL configurado. Falha de heartbeat marca o worker como stale: ele não inicia novos steps e para na próxima safe boundary; I/O externo já em voo termina apenas para registrar seu resultado via `execution_epoch`.

## 20.2. Cancelamento e restart seguros

Políticas para mensagem nova durante processamento:

```text
restart
queue
```

Cancelamento é **cooperativo**. `restart`/cancel nunca interrompe semanticamente a região:

```text
PREPARED -> EXECUTING -> external call -> FINALIZE/UNKNOWN
```

Uma mensagem nova define `cancel_requested`; o runtime interrompe apenas em fronteira segura de step. Side effects iniciados nunca são presumidos como desfeitos.

## 20.3. `action_id` e `logical_step_id`

`action_id` é criado uma única vez quando nasce uma `PendingAction` e é reutilizado até ela chegar a estado terminal. Uma ação equivalente já pendente é reutilizada quando `capability + args_hash + flow step` são iguais.

Para operações sem confirmação, o runtime cria `logical_step_id` persistente antes de executar qualquer tool. Em Flow:

```text
flow_instance_id + step_id + attempt_semantic_id
```

No agent loop aberto, `logical_step_id` é persistido no Turn Journal antes da execução. Nenhuma identidade de side effect depende apenas da resposta do LLM.

`attempt_semantic_id` muda **somente** quando existe uma nova tentativa decidida semanticamente pelo usuário ou pelo Flow, por exemplo após `business_error` ou um comando explícito como “tenta de novo”. Retry técnico, replay, reconciliação, timeout de transporte seguro ou reassignment de worker reutilizam o mesmo `attempt_semantic_id`, `logical_step_id` e idempotency key.

---

# 20A. Turn Journal

Cada turno possui um journal append-only. A identidade normativa de um step é:

```text
(turn_id, step_index, step_type)
```

Ele registra decisões necessárias para replay determinístico e auditoria operacional.

Tipos mínimos:

```text
INBOUND_AGGREGATED
OWNERSHIP_CHECK
UNDERSTANDING
ROUTE_DECISION
LLM_REQUEST
LLM_RESPONSE
SLOT_UPDATE
FLOW_TRANSITION
CAPABILITY_REQUEST
POLICY_DECISION
CONFIRMATION_DECISION
TOOL_PREPARED
TOOL_RESULT
COMPOSITION
TURN_COMPLETED
```

Estrutura conceitual:

```text
turn_journal

tenant_id
conversation_id
turn_id
step_index
step_type
logical_step_id?
request_hash?
payload
created_at
UNIQUE(turn_id, step_index)
```

Embora a constraint física possa continuar `UNIQUE(turn_id, step_index)`, o replay compara também `step_type` e `request_hash`.

Antes de executar um step, o runtime:

1. procura `(turn_id, step_index)`;
2. exige `stored.step_type == expected.step_type`;
3. para steps dependentes de request, exige `stored.request_hash == expected.request_hash`;
4. se tudo for compatível, reproduz o resultado persistido;
5. se houver divergência, lança `JournalDivergenceError`, marca o turno como falho e emite alerta — **nunca reexecuta silenciosamente**.

`INBOUND_AGGREGATED` persiste `turn_reference_time`, obtido do `Clock` na criação do turno. Toda resolução temporal do turno (“amanhã”, TTL derivado daquele step, etc.) usa esse valor imutável durante replay, e não um novo `Clock.now()`.

O journal é a base do replay determinístico. Cassettes de `ReplayLLM` podem ser derivados dele, mas o journal continua sendo o registro runtime autoritativo do turno.

Retenção de payload sensível é separada da retenção estrutural. Na v1:

- enquanto o turno está aberto, payloads necessários ao replay permanecem completos/redigidos de forma reversível por referência segura;
- após `COMPLETED/FAILED/CANCELLED`, inicia `journal_replay_payload_retention` configurável por tenant (default inicial: 7 dias);
- ao fim dessa janela, `LLM_REQUEST`, conteúdo inbound e tool payloads sensíveis são reduzidos para hashes, metadados e referências permitidas pela política de retenção;
- chaves estruturais, tipos de step, hashes e audit metadata podem ter retenção maior sem preservar PII bruta.

Determinismo não implica reter PII indefinidamente.

# 21. Extração e entendimento

A v1 evita dividir rigidamente o sistema em “NLU clássico” versus “LLM”.

O pipeline de entendimento é híbrido:

```text
raw messages
    ↓
normalizers / deterministic extractors
    ↓
structured LLM proposal when needed
    ↓
schema validation
    ↓
domain-independent validators
    ↓
Understanding + SlotUpdates
```

Entidades determinísticas importantes em pt-BR:

- datas relativas;
- horários;
- intervalos;
- duração;
- telefone;
- CPF/CNPJ;
- valores;
- e-mail.

Embeddings para classificação de intent entram apenas se houver evidência de benefício de custo/latência/qualidade.

---

# 22. Clock e datas determinísticas

O runtime injeta tempo confiável.

```python
class Clock(Protocol):
    def now(self) -> datetime: ...
```

Timezone é explícito.

O LLM pode identificar a expressão “amanhã às 15h”, mas a resolução final passa pelo normalizador determinístico usando `Clock` + timezone.

`Clock` também governa:

- TTL;
- debounce;
- leases;
- confirmation expiry;
- timers;
- políticas temporais.

---

# 23. Flows

Flows são state machines conversacionais, não scripts de integração.

Eles podem:

- exigir slots;
- validar slots;
- invocar capabilities;
- aguardar confirmação;
- reagir a resultados de tool;
- suspender/resumir;
- tratar cancelamento;
- tratar erro/timeout/unknown;
- permitir digressões controladas.

## 23.1. Flow ativo tem precedência

Quando existe `active_flow`, o Router tenta primeiro interpretar o novo turno como:

1. continuação do flow;
2. correção de slot;
3. confirmação/rejeição/modificação;
4. cancelamento do flow;
5. interrupção global/handoff;
6. digressão permitida.

Somente depois decide iniciar um flow totalmente novo.

---

## 23.2. Correção de slot

Exemplo:

```text
Usuário: Quero terça às 15h.
...
Usuário: Na verdade quinta.
```

O runtime atualiza o slot e invalida:

- slots derivados que dependam dele;
- resultados transitórios dependentes;
- PendingAction cujo `args_hash` mudou.

---

## 23.3. Digressão

Exemplo:

```text
flow de agendamento ativo
Usuário: Quanto custa mesmo?
```

O runtime pode executar uma intent global/FAQ permitida, responder e depois retornar ao flow anterior.

Flow stack/suspension deve ser explícito; não é inferido apenas pelo prompt.

---

## 23.4. Erros e timeouts

Cada step de capability define transições para a taxonomia canônica:

```text
success
validation_error
business_error
policy_denied
technical_error
timeout
unknown
```

Semântica normativa:

| Resultado | Significado | Retry automático |
|---|---|---|
| `validation_error` | argumentos/contrato inválidos antes da operação | não |
| `business_error` | sistema externo respondeu validamente e recusou por regra de negócio | não |
| `policy_denied` | runtime bloqueou antes da execução | não |
| `technical_error` | falha técnica cuja não-execução é conhecida ou operação é safe/idempotent | conforme policy |
| `timeout` | prazo excedido; classificação final depende de risco/idempotência | somente quando comprovadamente seguro |
| `unknown` | side effect pode ter ocorrido | **nunca retry cego** |

Para writes/irreversible, timeout ou 5xx após possível envio normalmente é `unknown` salvo garantia explícita do provider.

A v1 possui defaults seguros, e `error_map` do binding pode especializar códigos concretos sem reduzir segurança.

---

## 23.5. DSL: provar antes de serializar

O DSL é um risco de design. Portanto:

**primeiro:** representar Flow com objetos Python tipados / builder API.

**depois:** quando o modelo estiver estável, criar uma serialização YAML declarativa.

Exemplo futuro:

```yaml
flows:
  book_service:
    slots:
      service: {required: true}
      desired_date: {required: true}
      desired_time: {required: true}

    steps:
      - collect: [service, desired_date]
      - invoke: scheduling.availability
      - collect: desired_time
      - request_confirmation: scheduling.create
      - invoke: scheduling.create
```

YAML não deve se tornar uma linguagem de programação geral.

---

# 24. Router

O Router só é consultado depois do **Conversation Ownership Gate**.

```text
Inbound Turn
   ↓
Ownership Gate
   ├── HUMAN           -> persist/context only; bot não roteia nem responde
   ├── HANDOFF_PENDING -> policy específica; normalmente sem automação de domínio
   └── BOT
          ↓
      PendingAction? -> ConfirmationInterpreter
          ↓ no
      Active Flow?   -> Flow continuation first
          ↓ no
      Router          -> intent / open conversation
```

O Router considera:

```text
conversation state
active flow
understanding
interrupt intents
agent policies
```

O agent loop e os flows compartilham o mesmo pipeline de capabilities/tools/policies.

# 25. Agent loop

Loop pequeno e previsível.

Responsabilidades:

- montar contexto;
- restringir capabilities disponíveis;
- pedir structured output;
- aceitar propostas de slot/intenção;
- solicitar capability calls;
- devolver resultados ao LLM;
- produzir resposta final.

Limites:

- steps;
- calls;
- tokens;
- custo;
- timeout total;
- tools/capabilities permitidas.

O LLM nunca chama Provider diretamente.

## 25.1. Contrato interno de LLM

O Engine trabalha somente com modelos canônicos próprios:

```text
LLMRequest
LLMMessage
LLMContentPart
LLMToolDefinition
LLMToolCall
LLMStructuredOutput
LLMResponse
LLMUsage
LLMStopReason
```

Adapters convertem esses tipos para/de Anthropic, OpenAI-compatible ou outro provider. Nenhum `content block`, `response item` ou tipo proprietário vaza para `core/engine`.

Cada `LLM_REQUEST` e `LLM_RESPONSE` recebe `step_index` no Turn Journal. O `LLM_REQUEST` é persistido **antes** da chamada externa e recebe um `llm_request_id` estável derivado de `turn_id + step_index + request_hash`. Em replay, o engine reutiliza `LLM_RESPONSE` persistida antes de chamar o provider novamente.

Se houver `LLM_REQUEST` sem `LLM_RESPONSE` após crash, o adapter reutiliza o mesmo `llm_request_id` quando o provider suportar idempotência/request dedupe. Sem suporte do provider, pode existir duplicação de custo na janela ambígua entre processamento remoto e persistência local da resposta; o design não promete exactly-once billing do LLM. Essa ambiguidade nunca autoriza repetição cega de side effects de negócio.

---

# 26. Durable Outbox

Respostas são persistidas antes do envio.

```text
state/ledger transition
       +
outbound rows
       ↓
SAME LOCAL TRANSACTION
       ↓
commit
       ↓
Sender Worker
       ↓
RelayPlane
```

Port de envio/claim:

```python
class OutboxStore(Protocol):
    async def claim_ready(self, ...) -> list[OutboundMessage]: ...
    async def mark_queued(self, outbox_id: str, ...) -> None: ...
    async def mark_accepted(self, outbox_id: str, provider_message_id: str | None, ...) -> None: ...
    async def mark_failed(self, outbox_id: str, ...) -> None: ...
    async def mark_unknown(self, outbox_id: str, ...) -> None: ...
```

As linhas são criadas via `ConversationUnitOfWork` para preservar atomicidade com o estado do turno.

Idempotency key de mensagem:

```text
hash(tenant_id, conversation_id, turn_id, message_index)
```

Retry reenviará o **mesmo payload persistido**.

## 26.1. Prompt de confirmação e evidência de envio

Ao criar uma `PendingAction`, a mesma UoW cria a linha de confirmação na Outbox, grava `outbox.action_id` e define `PendingAction.latest_prompt_outbox_id = outbox.id`. O sender não precisa alterar o estado da conversa.

Estados relevantes da Outbox:

```text
PENDING -> SENDING -> QUEUED -> ACCEPTED
                   \-> ACCEPTED
                   \-> FAILED
                   \-> UNKNOWN -> RECONCILING -> ACCEPTED | FAILED | SUPERSEDED
```

`ACCEPTED` significa que o RelayPlane/canal aceitou a mensagem para envio; **não significa entregue nem lida**. Delivery/read receipts são metadados posteriores e não fazem parte da garantia mínima.

Um prompt de confirmação só se torna elegível em `ACCEPTED`. `QUEUED`, `SENDING` e `UNKNOWN` não autorizam ação protegida.

Para `UNKNOWN`, o runtime tenta reconciliação. Se não conseguir provar `ACCEPTED` dentro do prazo configurado, cria um **novo prompt** com novo `outbox_id`, incrementa `confirmation_attempts` e marca a tentativa anterior como `SUPERSEDED` para fins de elegibilidade. Pergunta duplicada é preferível a executar uma ação sem prova de prompt elegível.

A avaliação temporal/reply-to segue §10. O sender reutiliza a mesma idempotency key somente para retry técnico da **mesma linha de Outbox**; um re-prompt semântico é uma nova linha com nova identidade.

---

# 27. Media inbound e transcrição

Inbound pode conter texto, áudio, imagem, documento ou referências de mídia.

O envelope normalizado preserva referências sem obrigar download imediato.

Port inicial para nota de voz:

```python
class Transcriber(Protocol):
    async def transcribe(
        self,
        media: MediaReference,
        context: TranscriptionContext,
    ) -> Transcript: ...
```

O pipeline pode transformar áudio em um `MessagePart(text=...)` antes do estágio de entendimento.

Mídia maior continua preferencialmente usando claim-check/referência, não payload binário dentro do estado conversacional.

---

# 28. Mensagens proativas

Mensagens proativas entram pelo mesmo runtime através de `ScheduledEvent`/system event.

```text
Scheduler
   ↓
Proactive Event
   ↓
Conversation Runtime
   ↓
Channel Policy
   ↓
Outbox
   ↓
RelayPlane
```

O envio respeita regras do canal, incluindo janelas de atendimento, templates quando exigidos e rate policies do RelayPlane.

O runtime não deve hard-code regras específicas de WhatsApp dentro do core; elas pertencem a uma policy/capability do canal.

---

# 29. Human handoff

Ownership explícito:

```text
BOT
HANDOFF_PENDING
HUMAN
```

Em `HUMAN`:

- eventos continuam persistidos;
- timers podem continuar;
- o bot não responde automaticamente;
- retorno a BOT exige evento/policy explícita.

Timeouts de handoff usam `Scheduler`, não timer em memória.

---

# 30. Segurança do HTTPToolProvider

Tools HTTP têm risco de SSRF e exfiltração de secrets.

Regra estrutural:

> **O LLM e a Tool Definition não fornecem URL absoluta arbitrária em runtime.**

O binding referencia uma conexão pré-cadastrada:

```yaml
provider:
  type: http
  connection: scheduling_api

request:
  method: POST
  path: /appointments
```

`connection` define:

- base URL permitida;
- hosts/ports permitidos;
- TLS policy;
- auth method;
- secret references;
- timeout máximo;
- limites de payload;
- redirects permitidos ou desabilitados;
- política de DNS/private networks.

Defesas:

- allowlist de host;
- canonicalização de URL;
- bloqueio de override de scheme/host pelo modelo;
- proteção contra DNS rebinding;
- recusa de redes privadas/reservadas quando a connection não autorizar explicitamente;
- redirects restritos;
- tamanho máximo de request/response;
- redaction de secrets em logs.

---

# 31. Secrets e connections por tenant

Credenciais não ficam no AgentDefinition em texto puro.

Ports:

```python
class ConnectionResolver(Protocol):
    async def resolve(self, tenant_id: str, connection_id: str) -> ResolvedConnection: ...


class SecretProvider(Protocol):
    async def get(self, tenant_id: str, secret_ref: str) -> SecretValue: ...
```

A mesma Tool Definition pode usar credenciais diferentes por tenant.

Secrets nunca entram no prompt, traces ou `args_hash`.

---

# 32. Tool discovery

Tool definitions podem vir de:

- arquivo;
- registry;
- MCP;
- OpenAPI;
- composição Python.

Descoberta não implica exposição ao LLM.

OpenAPI/MCP sempre usam allowlist explícita.

Importadores podem gerar Tool Definitions, mas bindings/capabilities permanecem decisões do agente.

---

# 33. Packs

Packs entram somente como composição/reuso.

Manifesto conceitual:

```python
class PackManifest(BaseModel):
    name: str
    version: str
    intents: list[IntentDefinition]
    flows: list[FlowDefinition]
    requirements: CapabilityRequirements
    policies: list[PolicyDefinition]
    prompts: list[PromptFragment]
    evals: list[EvalScenario]
```

Um Pack:

- não implementa API externa;
- não possui business state;
- não ganha acesso direto a adapters;
- não executa tools fora do pipeline;
- pode ser criado por terceiros.

Depois da compilação, o Engine não precisa saber se uma definição veio de Pack ou localmente.

---

# 34. Agent Compiler

Depois que o modelo Python estiver provado, o compiler resolve YAML/manifestos e valida:

- duplicidade de nomes;
- capability requirements;
- bindings;
- input/output maps;
- compatibilidade de schema;
- risk/confirmation efetivos;
- tentativa de downgrade de segurança;
- references de flows;
- policies;
- Pack versions;
- connections ausentes;
- tools ausentes;
- prompt fragments;
- limites do runtime.

Saída:

```text
CompiledAgent
```

imutável durante um turno.

---

# 35. Versionamento e migração de conversa

Cada turno registra:

```text
tenant_id
agent_id
agent_version
prompt_version
pack_versions
runtime_version
```

Política padrão:

> **uma sessão/flow em andamento permanece pinada na versão com a qual começou.**

Novas sessões usam a versão publicada mais recente.

Migração de sessão em andamento é operação explícita e precisa definir:

- transformador de state schema;
- compatibilidade de flow;
- destino para PendingAction;
- comportamento para timers existentes.

Nunca ocorre hot migration silenciosa no meio de uma ação pendente.

---

# 36. Ciclo de vida de conversa e sessão

Distinguir:

```text
contact_id       # identidade do contato no escopo do tenant/canal
conversation_id  # thread/canal lógico
session_id       # janela conversacional operacional
```

Uma mesma conversa pode ter múltiplas sessões ao longo do tempo.

Session rollover pode ocorrer por:

- timeout de inatividade;
- encerramento explícito;
- handoff finalizado;
- regra do canal/projeto.

O estado de flow é session-scoped por padrão.

**Grupos ficam fora do escopo inicial**, salvo decisão explícita posterior.

---

# 37. Multi-tenancy

Identidade estrutural:

```text
tenant_id
agent_id
channel_id
conversation_id
session_id
contact_id
```

Todos os repositórios persistentes particionam/autorizam por `tenant_id`.

Nenhum desses IDs críticos é inferido pelo LLM.

---

# 38. LGPD, retenção e apagamento

A v1 precisa modelar retenção desde cedo.

Configuração por tenant:

```text
message_retention
conversation_state_retention
trace_retention
memory_retention
media_reference_retention
```

Requisitos:

- PII redaction em logs/traces;
- purge de state/memory por contato;
- deleção/anonimização de mensagens quando aplicável;
- índice que permita localizar dados por `tenant_id + contact_id`;
- audit trail mínimo de operações administrativas sem reter conteúdo desnecessário;
- secrets nunca persistidos em traces.

A remoção em sistemas externos de negócio pertence aos próprios sistemas, mas o runtime pode expor hooks/capabilities administrativas para coordenar processos quando necessário.

---

# 39. Ports

## 39.1. Primary ports

```text
IngestEvent
ProcessTurn
ProcessScheduledEvent
CompileAgent
RunEvaluation
```

## 39.2. Secondary ports

```text
LLMProvider
ConversationUnitOfWorkFactory
ToolInvocationStore
InboxStore
OutboxStore
Memory
MessageSender
ToolProvider
ToolCatalog
ConnectionResolver
SecretProvider
Scheduler
Tracer
Clock
Transcriber
PackLoader
```

Implementações PostgreSQL podem compartilhar internamente a mesma conexão/transação quando necessário, sem expor detalhe de adapter ao engine.

---

# 39A. Modelos canônicos do runtime

Os seguintes modelos formam o vocabulário estável entre engine, ports, adapters e testes:

```text
InboundEvent
NormalizedMessage
MessagePart
MediaReference
ConversationState
SessionState
Turn
TurnJournalEntry
Understanding
SlotUpdate
IntentProposal
CapabilityDefinition
CapabilityRequest
CapabilityResult
ToolDefinition
ResolvedToolBinding
ToolInvocation
ToolResult
PendingAction
ActionConfirmation
PolicyDecision
OutboundMessage
ScheduledEvent
CompiledAgent
```

Modelo mínimo de resultado de tool:

```python
class ToolResult(BaseModel):
    status: Literal[
        "success",
        "validation_error",
        "business_error",
        "policy_denied",
        "technical_error",
        "timeout",
        "unknown",
    ]
    data: dict | list | None = None
    error: ToolError | None = None
    provider_metadata: dict = {}
```

`ToolError` possui ao menos `code`, `message_safe`, `retryable`, `details_ref?`; nunca expõe secret ou body bruto não confiável ao prompt por padrão.

---

# 39B. State machines normativas

## Turn

```text
PENDING -> PROCESSING -> COMPLETED
                    \-> FAILED
                    \-> CANCELLED
```

O Turn não permanece aberto esperando o usuário. Quando uma confirmação é necessária, o turno cria `PendingAction + Outbox`, faz commit e termina `COMPLETED`; a resposta do usuário inicia outro turno. `COMPLETED/FAILED/CANCELLED` são terminais. Replay do mesmo turno consulta o journal.

## ToolInvocation

```text
PREPARED -> EXECUTING -> SUCCEEDED
                      -> FAILED
                      -> UNKNOWN -> RECONCILING -> RECONCILED
                                             \-> FAILED
                                             \-> HUMAN_HANDOFF
```

`SUCCEEDED`, `FAILED` terminal e `RECONCILED` terminal não regressam para `EXECUTING`.

## InboxEvent

```text
RECEIVED -> READY -> CLAIMED -> CONSUMED
                     \-> RETRY
                     \-> DEAD
```

## OutboxMessage

```text
PENDING -> SENDING -> QUEUED -> ACCEPTED
                   \-> ACCEPTED
                   \-> FAILED
                   \-> UNKNOWN -> RECONCILING -> ACCEPTED
                                                \-> FAILED
                                                \-> SUPERSEDED
```

`ACCEPTED` não implica delivery/read.

## Session ownership

```text
BOT -> HANDOFF_PENDING -> HUMAN
 ^                         |
 +------ explicit return --+
```

## PendingAction

A state machine é a definida no §10; `CONFIRMED` só pode ser commitada com `ToolInvocation(PREPARED)` correspondente.

---

# 39C. Persistência PostgreSQL v1

Topologia mínima:

```text
tenants
agents
agent_versions
contacts
conversations
sessions
conversation_states
turns
turn_journal
inbox_events
outbox_messages
pending_actions
action_confirmations
tool_invocations
scheduled_events
```

Constraints mínimas:

```text
UNIQUE(tenant_id, channel_id, event_id)                  -- inbox dedupe
UNIQUE(tenant_id, invocation_id)                         -- tool identity
UNIQUE(tenant_id, conversation_id, turn_id, step_index) -- journal
UNIQUE(tenant_id, conversation_id, turn_id, message_index) -- outbox
UNIQUE(tenant_id, scheduler_key)                         -- timers
UNIQUE(tenant_id, action_id)                             -- protected action
```

`outbox_messages` inclui `action_id?`, `provider_message_id?`, `provider_accepted_at?`, `status` e timestamps de transição para permitir prova de confirmação e reconciliação.

Campos que participam de claim/retry/auditoria ficam normalizados. Estado agregado de flow/slots pode usar JSONB versionado, desde que migrations de schema sejam explícitas.

`conversation_states` inclui no mínimo:

```text
version
conversation_epoch
lease_owner
lease_expires_at
ownership
active_flow
flow_state_json
agent_version
updated_at
```

Índices devem suportar `tenant_id + contact_id` para retenção/LGPD e filas por `status + due_at/available_at`.

---

# 39D. Algoritmo normativo de um turno

O `TurnCoordinator` segue esta ordem lógica. Steps já presentes no journal são reproduzidos, não recalculados.

```text
01. selecionar conversation candidate a partir de Inbox/Scheduler sem claim destrutivo
02. resolver tenant, agent, conversation, session, contact a partir do envelope persistido
03. acquire conversation lease + increment/obtain conversation_epoch
      se falhar -> nenhum evento é claimado; outro worker é o owner
04. claim inbound/scheduled events SOMENTE dessa conversa sob o lease atual
      se claim vazio -> liberar lease e encerrar
05. ordenar eventos claimados e formar burst
06. create/load Turn e Turn Journal
      persistir INBOUND_AGGREGATED + turn_reference_time
07. Ownership Gate
      HUMAN -> persistir contexto e encerrar automação sem outbound automático
08. carregar CompiledAgent pinado à sessão/flow
09. normalizar mídia/transcrição quando aplicável
10. se PendingAction aguardando resposta -> ConfirmationInterpreter
      confirm -> na MESMA UoW: ActionConfirmation + CONFIRMED + PREPARED + journal commit
      modify/reject/unclear -> atualizar state/outbox/journal commit
11. senão executar understanding/slot extraction
12. se Flow ativo -> FlowRunner primeiro
13. senão Router -> Flow ou AgentLoop
14. qualquer CapabilityRequest -> resolve Binding
15. PolicyGate
      DENY -> journal/state/outbox commit
      REQUIRE_CONFIRMATION -> criar PendingAction + prompt Outbox commit e encerrar turn step
      ALLOW -> continuar
16. persistir logical_step_id/attempt_semantic_id + ToolInvocation(PREPARED) + journal; commit com conversation_epoch
17. fora de transação da conversa: claim invocation -> EXECUTING, obtendo execution_epoch
18. chamar ToolProvider
19. classificar resultado via provider + error_map
20. finalize o FATO EXTERNO no ledger com execution_epoch:
      SUCCEEDED / FAILED / UNKNOWN + result/error
      result_application_status = pending
21. adquirir/validar conversation_epoch atual
      se o worker perdeu o lease -> não aplicar state; novo owner/reconciliation continua
22. UoW da conversa:
      ler terminal ledger result
      persist ToolResult + Flow transition + journal
      result_application_status = applied
23. continuar Flow/AgentLoop se budget permitir
24. compor mensagens
25. persist ConversationState + Turn terminal + Outbox + journal na mesma UoW
26. commit com conversation_epoch fencing
27. mark claimed inbox events CONSUMED no mesmo boundary local definido pelo adapter/UoW
28. liberar lease / worker encerra
29. Outbox Sender entrega de forma independente
```

Nenhuma chamada LLM ou HTTP externa mantém a transação PostgreSQL da conversa aberta.

---

# 40. RelayPlane

Integração inicial:

```text
WhatsApp / channel
      ↓
RelayPlane
      ↓ subscription/webhook
Conversation Agent
      ↓ MessageSender
RelayPlane
```

Inbound:

- assinatura HMAC;
- dedupe por event id;
- at-least-once;
- persistência antes de 2xx;
- ordering best effort com metadados do evento.

Outbound:

- durable outbox;
- idempotency key estável;
- retry pelo sender;
- rate policy no RelayPlane/canal.

Mapeamento mínimo de status:

| RelayPlane | Outbox | Confirmação elegível? |
|---|---|---:|
| `QUEUED` | `QUEUED` | não |
| `ACCEPTED` | `ACCEPTED` | sim, sujeito à evidência do §10 |
| `FAILED` | `FAILED` | não |
| `UNKNOWN` | `UNKNOWN` | não |

`ACCEPTED` significa aceite para envio, não delivery/read.

A retenção temporal da deduplicação por `Idempotency-Key` do RelayPlane **ainda não possui garantia formal documentada**. Portanto a integração de produção exige um contrato/configuração explícita `relayplane_idempotency_retention` e a seguinte relação:

```text
sender_retry_horizon <= relayplane_idempotency_retention
```

Se uma Outbox ultrapassar essa janela sem estado terminal conhecido, o sender não pode simplesmente reenviar contando com a mesma key; deve reconciliar status ou escalar. A Fase 6 inclui um contract test que mede/valida essa garantia contra o RelayPlane implantado.

---

# 41. Observabilidade

Trace conceitual:

```text
turn
 ├── inbox.claim
 ├── normalize.media
 ├── transcribe?
 ├── debounce
 ├── understand
 ├── route
 ├── flow/agent.step
 ├── confirmation
 ├── policy
 ├── tool.prepare
 ├── tool.execute
 ├── tool.reconcile?
 ├── state.commit
 ├── compose
 └── outbox.send
```

Métricas:

```text
turn latency
LLM latency/tokens/cost
confirmation rate
tool latency/error/unknown rate
reconciliation success rate
policy denials
handoff rate
inbox backlog
outbox backlog
scheduler lag
late event count
retry count
conversation volume
```

PII e secrets são redigidos antes de logs/traces.

---

# 42. Testabilidade

## 42.1. Fakes e replay

```text
FakeLLM
ReplayLLM
FakeClock
FakeToolProvider
ReplayToolProvider
FakeScheduler
FakeTranscriber
```

## 42.2. Contract suites

```text
ConversationUnitOfWork contract
InboxStore contract
OutboxStore contract
ToolProvider contract
MessageSender contract
Scheduler contract
LLMProvider contract
Transcriber contract
PackLoader contract
```

## 42.3. Chaos tests

O runtime possui fault injection com casos nomeados (`C01`–`C16`): kills entre cada etapa do protocolo de side effect, zumbi de lease, cancelamento em voo, confirmação com prompt ambíguo, divergência de journal e reconciliação. A tabela completa, a fase em que cada caso passa a ser exigido e o resultado esperado ficam em [`ROADMAP.md`](./ROADMAP.md#chaos-gates) (fonte única); o mapa invariante → teste fica em [`INVARIANTS.md`](./INVARIANTS.md).

---

# 43. Evals

Evals podem validar comportamento conversacional e invariantes.

Exemplo:

```yaml
scenario: protected_action_requires_confirmation

messages:
  - user: "Quero amanhã às 15h"

expect:
  state:
    pending_action: true

  forbidden_capabilities:
    - scheduling.create
```

Exemplo de modificação:

```yaml
scenario: change_after_confirmation_prompt

messages:
  - user: "Quero amanhã às 15h"
  - assistant: "Confirma amanhã às 15h?"
  - user: "Sim, mas às 16h"

expect:
  old_pending_action: invalidated
  capability_not_executed: scheduling.create
  new_slot:
    desired_time: "16:00"
```

LLM judge é opcional; invariantes críticas são assertions determinísticas.

---

# 44. API de referência para o primeiro agente

O repositório inclui uma API externa de exemplo, propositalmente fora do runtime:

```text
examples/reference-scheduling-api/
```

Endpoints mínimos:

```text
GET  /availability
POST /bookings
GET  /bookings/by-idempotency-key/{key}
DELETE /bookings/{id}
```

Ela é source of truth do domínio e deve implementar:

- constraint contra double booking;
- idempotency key em create;
- status lookup;
- erros de negócio explícitos.

O objetivo não é criar um produto de scheduling, mas provar que o Conversation Agent consegue operar sobre um sistema externo real através de HTTP.

---

# 45. Estrutura do repositório

```text
conversation_agent/
  src/conversation_agent/

    core/
      models/
      definitions/
      errors/

    ports/
      llm.py
      uow.py
      inbox.py
      outbox.py
      memory.py
      sender.py
      tool_provider.py
      tool_catalog.py
      connections.py
      secrets.py
      scheduler.py
      tracing.py
      clock.py
      transcriber.py
      pack_loader.py

    engine/
      ingest.py
      burst.py
      turn_coordinator.py
      understand.py
      router.py
      flow_runner.py
      agent_loop.py
      confirmation.py
      policy_gate.py
      tool_runner.py
      reconciliation.py
      compositor.py
      handoff.py
      proactive.py

    tools/
      definitions.py
      bindings.py
      mapping.py
      risk.py
      invocation.py

    compiler/
      agent_compiler.py
      validation.py
      resolver.py

    packs/
      manifest.py
      loader.py

    adapters/
      postgres/
        uow.py
        inbox.py
        outbox.py
        scheduler.py

      llm/
        anthropic.py
        openai_compat.py
        fake.py
        replay.py

      inbound/
        cli.py
        fastapi.py
        relayplane.py

      senders/
        cli.py
        relayplane.py

      tools/
        http.py
        mcp.py
        python.py
        fake.py
        replay.py

      transcription/
        ...

      tracing/
        opentelemetry.py

    evals/
      runner.py
      assertions.py
      judge.py
      cassettes.py

    app/
      cli.py
      api.py
      worker.py
      scheduler_worker.py
      composition_root.py

  examples/
    vertical-slice/
    reference-scheduling-api/
    scheduling-agent/
    faq-support-agent/

  tests/
    architecture/
    contracts/
    engine/
    chaos/
    evals/
    compiler/

  docs/
```

---

# 46. Fronteiras de dependência

Verificadas com `import-linter`.

```text
core
 ↑
ports
 ↑
engine / compiler
```

Adapters dependem de `core + ports`, nunca o contrário.

Engine não importa:

```text
Anthropic
OpenAI
RelayPlane
Postgres
HTTPX
MCP SDK
ERP SDK
CRM SDK
```

Packs não recebem acesso privilegiado ao engine/adapters.

---

# 47. Processo para construir um chatbot

Depois que o framework estiver maduro:

```text
1. Definir persona e comportamento
2. Declarar/importar Flows
3. Declarar/importar Capability contracts
4. Registrar Tool Definitions
5. Configurar Connections/Secrets
6. Fazer Capability Bindings + mappings
7. Adicionar Policies
8. Opcionalmente importar Packs
9. Criar evals
10. Compilar AgentDefinition
11. Rodar localmente
12. Executar evals + chaos/contract tests aplicáveis
13. Publicar versão
14. Associar tenant/canal
```

Durante o bootstrap do projeto, os mesmos objetos podem ser montados diretamente em Python antes de existir YAML estável.

---

# 48. Developer Experience

CLI desejada após estabilização do modelo:

```bash
conversation-agent dev examples/scheduling-agent
conversation-agent validate agent.yaml
conversation-agent compile agent.yaml
conversation-agent eval agent.yaml
conversation-agent inspect agent.yaml
```

Inspeção local:

```text
/state
/flow
/pending
/invocations
/tools
/capabilities
/trace
/timers
/outbox
```

---

# 49. Fatias de entrega

Escopo da v1, fases 1–10, DoD, o gate GO/NO-GO após a Fase 1 e os chaos gates exigidos por fase ficam em [`ROADMAP.md`](./ROADMAP.md) (histórico; o roadmap vigente é [`04_ROADMAP.md`](./04_ROADMAP.md)). Resumo da ordem:

```text
1 Vertical slice (+ GO/NO-GO)  ->  2 Reliability core  ->  3 Protected actions + confirmation + security
-> 4 Flows e entendimento  ->  5 AgentDefinition + Compiler  ->  6 RelayPlane + mídia + proativo
-> 7 Tool ecosystem  ->  8 Primeiro Pack  ->  9 Segundo domínio  ->  10 Production hardening
```

---

# 50. Critérios de sucesso arquitetural

## 1. Vertical slice cedo

O projeto executa uma conversa completa com LLM + tool HTTP real antes de depender de compiler/DSL sofisticado.

## 2. Novo domínio sem tocar no engine

Um terceiro cria novo caso de uso apenas com definitions, capabilities, bindings, tools e policies.

## 3. API trocável

A mesma Capability/Flow funciona com duas APIs que possuem schemas diferentes, usando somente binding mappings.

## 4. Segurança não pode ser rebaixada

Capability/Tool/Binding/Policy sempre resultam no nível de proteção mais restritivo.

## 5. Confirmação é action-scoped

“Sim” somente autoriza exatamente a ação cuja `action_id`/`args_hash` foi apresentada.

## 6. Retry não duplica side effect

Crash/replay gera a mesma identidade para ação protegida.

## 7. `unknown` não faz retry cego

Toda tool protegida possui estratégia explícita de reconciliação ou escalonamento.

## 8. Estado + ledger + outbox são atomicamente consistentes localmente

Não existe estado dizendo “respondi” sem outbox correspondente, nem outbox commitada sem a transição local que a gerou.

## 9. Evento tardio não reescreve passado

Turns commitados são imutáveis; late events entram em turno posterior.

## 10. Timer sobrevive restart

Debounce, confirmation expiry, reconciliation e proativos não dependem de `asyncio.sleep` residente em processo.

## 11. Pack é opcional

O framework cria um agente completo sem Pack e permite Packs de terceiros sem extensão do engine.

## 12. Chaos suite passa

Fault injection nos pontos críticos mantém as invariantes de idempotência e entrega.

## 13. Worker obsoleto não faz commit

Lease expirada + novo `conversation_epoch` bloqueiam qualquer mutação do worker antigo.

## 14. Replay do turno é journal-driven

LLM/tool steps já persistidos não são executados novamente durante replay do mesmo turno.

## 15. Confirmação e prepare são atomicamente consistentes

Após commit, nunca existe confirmação aceita sem `ToolInvocation(PREPARED)` correspondente.

## 16. HUMAN permanece silencioso

Ownership `HUMAN` nunca gera resposta automática, capability call ou novo flow pelo bot.

## 17. Dedupe inbound precede criação de turno

O mesmo `tenant_id + channel_id + event_id` nunca cria dois turns, mesmo sob redelivery concorrente.

## 18. Ledger preserva o fato externo apesar de perda da conversa

Um executor com `execution_epoch` válido registra `SUCCEEDED/FAILED/UNKNOWN` mesmo após perder o `conversation_epoch`; somente a aplicação desse fato ao estado conversacional espera o novo owner.

## 19. Replay divergente falha fechado

`step_type` ou `request_hash` incompatível com journal existente gera erro explícito e alerta; nunca reexecuta silenciosamente.

---

# 51. Questões deliberadamente abertas

Devem ser decididas com implementação/evidência, não por antecipação excessiva:

- sintaxe final do Flow YAML;
- expressão final do mapping declarativo;
- `restart` vs `queue` como default;
- tamanho padrão do debounce;
- storage/registry final de AgentDefinitions;
- registry de Packs;
- sandbox para PythonToolProvider de terceiros;
- modelo de streaming;
- provider inicial de transcrição;
- política de long-term memory;
- uso de embeddings para intent;
- política de migration entre versões;
- regras específicas de cada canal;
- política de retenção estrutural do Turn Journal após scrub do payload de replay;
- default de `confirmation_attempts`;
- valores numéricos default de lease TTL/heartbeat por ambiente (o mecanismo de heartbeat é normativo).

---

# 52. Organização da documentação normativa

```text
docs/
  DESIGN.md           # visão consolidada e racional (este arquivo)
  INVARIANTS.md       # fonte única: invariantes + mapa INV -> teste
  RUNTIME_PROTOCOL.md # fonte única: lease, claim, epochs, journal, side effects, confirmation, outbox
  ROADMAP.md          # HISTÓRICO: fases, DoD, GO/NO-GO; referência viva só da tabela de chaos gates (C01–C16)
  README.md           # índice
```

Regra de precedência (substituída por ADR-020 em `03_DECISIONS.md`: código/testes > INVARIANTS > RUNTIME_PROTOCOL > canônicos 00–06 > este arquivo > IMPLEMENTATION_STATUS): o que estiver em `INVARIANTS.md` ou `RUNTIME_PROTOCOL.md` prevalece sobre este arquivo. Trechos normativos não devem ser duplicados aqui; o `DESIGN.md` explica o porquê e aponta para a fonte.

Mudanças de protocolo que alterem persistência, idempotência, fencing, confirmação ou semântica de retry exigem:

1. atualização da invariante correspondente;
2. atualização do protocolo;
3. teste nomeado/chaos case;
4. atualização do roadmap/DoD quando afetar uma fase.

---

# 53. Regra final

```text
Conversation Runtime
    owns orchestration, reliability and conversational state

Business Systems
    own domain state and business truth

Tools
    expose external operations

Capabilities
    provide stable semantic contracts

Bindings
    adapt semantic contracts to concrete tools

Packs
    optionally package reusable behavior
```

Em uma frase:

> **O framework orquestra com garantias explícitas de confiabilidade e replay; capabilities descrevem intenção operacional; bindings adaptam; tools integram; sistemas externos são a verdade do negócio; Packs apenas reutilizam comportamento.**
