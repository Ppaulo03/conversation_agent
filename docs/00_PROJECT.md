# 00 — Project

> Memória canônica do projeto. Hierarquia: [`00_PROJECT`](./00_PROJECT.md) (o que) ·
> [`01_METHODOLOGY`](./01_METHODOLOGY.md) (como trabalhamos) · [`02_ARCHITECTURE`](./02_ARCHITECTURE.md) (como funciona agora) ·
> [`03_DECISIONS`](./03_DECISIONS.md) (por quê) · [`04_ROADMAP`](./04_ROADMAP.md) (para onde vamos) ·
> [`05_REVIEW_LOG`](./05_REVIEW_LOG.md) (o que está tecnicamente aberto) · [`06_AGENT_HANDOFF`](./06_AGENT_HANDOFF.md) (como delegamos).
> Este arquivo é curto de propósito: histórico detalhado fica em [`IMPLEMENTATION_STATUS.md`](../IMPLEMENTATION_STATUS.md).

## O que estamos construindo

`conversation_agent`: framework Python para **agentes conversacionais stateful**. Ele é dono do **estado conversacional**,
nunca do estado de negócio (INV-008). Um agente é declarado como dado (manifest YAML + allowlist), compilado, publicado como
versão imutável e executado por um runtime durável sobre PostgreSQL, com um canal de mensagens (hoje o gateway RelayPlane,
mais um canal de terminal para desenvolvimento).

O caso guia é um agente de agendamento de serviços (quadras) contra uma API externa; um segundo domínio (suporte/ticket)
prova que o framework não é de agendamento.

## Objetivos

- Conduzir conversas multi-turn com LLM, Flows e tools **sem executar efeito colateral que o contato não confirmou**.
- Garantias explícitas de confiabilidade: nenhum efeito externo duplicado, perdido sem rastro ou executado às cegas após
  ambiguidade (`UNKNOWN` é estado de primeira classe).
- Replay determinístico de turnos (journal) e recuperação após crash sem repetir LLM nem tool.
- Domínios novos como dado (manifest + allowlist + bindings), sem tocar `src/`.
- Operabilidade: observabilidade, custo de LLM, SLOs, releases/canary, LGPD (retenção e apagamento), migrations forward-only.

## Non-goals

- Ser dono de reservas, pedidos, tickets, pagamentos ou qualquer estado de negócio (INV-008).
- Plugins Python de terceiros não confiáveis (sandbox), streaming, memória de longo prazo, embeddings para intent,
  FAQ/RAG (`docs/DESIGN.md` §51 e ROADMAP legado "Não bloqueiam v1").
- Mídia de saída (o agente só envia texto).
- Grupos e visão de imagens: **não são non-goals definitivos, mas também não são compromisso**. São capacidades candidatas
  futuras ([`FUTURE_GROUPS.md`](./FUTURE_GROUPS.md), [`FUTURE_VISION.md`](./FUTURE_VISION.md)); ver [`04_ROADMAP`](./04_ROADMAP.md).

## Princípios fundamentais

1. Estado conversacional ≠ estado de negócio; o framework orquestra, tools integram.
2. O LLM não é autoridade: não fornece identidade, segredos nem confirmação; conteúdo de mensagem/tool é dado não confiável (INV-002, INV-013).
3. Toda operação externa cruza `Capability → Binding → PolicyGate → ToolRunner` (INV-003).
4. Ação protegida só executa com confirmação provadamente posterior ao prompt entregue (INV-004/005/022).
5. Escrita com resultado ambíguo nunca sofre retry cego (INV-006/021).
6. Banco é a autoridade de tempo de coordenação; fences separados para conversa e execução (INV-009/011/016/032).
7. Observabilidade e custo são canal lateral e nunca carregam conteúdo ou pessoa (INV-048/049).
8. O código e os testes são a verdade final; documentação divergente é defeito ([metodologia §2.6](./01_METHODOLOGY.md)).

Normativo e completo: [`INVARIANTS.md`](./INVARIANTS.md) (INV-001…INV-071, cada uma com teste nomeado).

## Capacidades atuais (alto nível)

Runtime durável (inbox/outbox/journal/ledger/scheduler/leases), ações protegidas com confirmação, Flows tipados com
extração de slots e datas pt-BR, handoff para humano, AgentDefinition + compiler + registry versionado + releases
(stable/canary/rollback), Packs de dados, providers de tools (HTTP guardado, MCP, replay), adapter RelayPlane (webhook HMAC,
sender com idempotência, mídia por referência), transcrição de voz opt-in, mídia como argumento de tool, proativos/timers,
backpressure, LGPD (retenção/erasure), auditoria administrativa, logs/spans/métricas Prometheus/ledger de custo de LLM
com orçamento (`alert|degrade|refuse_new`), eval harness offline, e entrypoint terminal (`app.serve`) para execução local/desenvolvimento. Não existe entrypoint first-party de produção com RelayPlane (decisão em aberto, ver `04_ROADMAP`).
Como funciona: [`02_ARCHITECTURE`](./02_ARCHITECTURE.md).

## Estado atual

- Fases legadas 1–15b implementadas em `main` (histórico: `IMPLEMENTATION_STATUS.md`); visão e grupos **não** implementados.
- Iteration **Baseline & Engineering Consolidation**: aceita (sem mudança de comportamento). Evidência do baseline e
  findings abertos: [`05_REVIEW_LOG`](./05_REVIEW_LOG.md).
- Ainda **sem evidência executada** de: contrato do gateway RelayPlane de produção, ensaio de restore/DR, exporter de métricas
  agregadas/OpenTelemetry, pipeline de CI no repositório. Ver findings F-002, F-003, F-005 e F-007.

## Principais restrições

- Python ≥ 3.12 (testado em 3.13), PostgreSQL 16 (`asyncpg`; o psycopg async não roda no ProactorEventLoop do Windows).
- Dependências de fronteira verificadas por import-linter (`core ← ports ← tools ← engine`; adapters nunca importam engine/app; `examples/` nunca é importado por `src/`).
- Migrations só avançam (forward-only, checksum).
- Segurança de entrega depende do contrato do gateway: `sender_retry_horizon <= idempotency_retention` ([`RELAYPLANE_CONTRACT.md`](./RELAYPLANE_CONTRACT.md)).
- Testes live com LLM têm orçamento diário baixo (`tests/support/live_budget.py`).
