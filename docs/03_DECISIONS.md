# 03 — Decisions

Registro de decisões arquiteturais **duráveis e já demonstráveis** (código + teste). Formato da
[metodologia §25](./01_METHODOLOGY.md). Regras desta versão:

- Cada ADR cita a evidência (invariante/teste/seção do status legado). **O racional só reproduz o que está escrito nos documentos
  históricos** ([`DESIGN.md`](./DESIGN.md), [`IMPLEMENTATION_STATUS.md`](../IMPLEMENTATION_STATUS.md), [`RUNTIME_PROTOCOL.md`](./RUNTIME_PROTOCOL.md)); onde o
  racional não está registrado, o ADR diz isso em vez de inventá-lo.
- Alternativas só aparecem quando o material legado as menciona.
- Esta iteração (Baseline & Engineering Consolidation) **formaliza** decisões existentes; a única decisão nova é de governança
  documental (ADR-020). Nenhuma decisão de runtime foi criada.
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
Decision: sem scripts down; voltar = restore ou migration nova (expand/contract); drift ou banco à frente impedem iniciar; apply serializado por advisory lock.
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

---

## Questões em aberto (não são decisões)

- A lista de “questões deliberadamente abertas” em [`DESIGN.md` §51](./DESIGN.md) **não foi reauditada** nesta iteration contra o código; alguns itens
  (provider de transcrição, registry de AgentDefinitions, política de migration entre versões, debounce) foram resolvidos por fases posteriores, outros (streaming, memória de longo prazo, sandbox de
  tool Python de terceiros, embeddings) seguem como non-goals. Reauditar é trabalho de uma iteration futura, não um fato aqui afirmado.
- Valores numéricos de produção de lease TTL, heartbeat, `confirmation_attempts`, DeliveryPolicy: dependem de ambiente; não há decisão registrada.
- Prioridade de Grupos e Visão: **sem decisão** (ver [`04_ROADMAP`](./04_ROADMAP.md)).
