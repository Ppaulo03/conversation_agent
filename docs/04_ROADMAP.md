# 04 — Roadmap (para onde vamos)

Só trabalho futuro e a direção atual. **Não é histórico**: o que já foi implementado está em
[`IMPLEMENTATION_STATUS.md`](../IMPLEMENTATION_STATUS.md) (log) e descrito em [`02_ARCHITECTURE`](./02_ARCHITECTURE.md). O
[`ROADMAP.md`](./ROADMAP.md) legado é histórico de fases (mantém só a tabela de chaos gates como referência viva, ver ADR-020).
Itens abaixo são **direção recomendada**, não compromissos de escopo, até decisão explícita do Product Owner.

## Direção atual

```text
Baseline & Engineering Consolidation   (esta iteration — sem mudança de comportamento)
    ↓
Production Delivery
    ↓
operational evidence
    ↓
decisão explícita sobre a próxima capability
```

### 1. Baseline & Engineering Consolidation — aceita

Entregue: baseline do repositório verificável (F-001 `RESOLVED`), memória canônica `00`–`06`, autoridade documental reconciliada
(F-004 `RESOLVED`). Os findings de evidência operacional (F-002, F-003, F-005, F-007) continuam `OPEN` e alimentam a próxima iteration.

### 2. Production Delivery — próxima iteration (candidata, aguardando decisão de escopo)

Objetivo: levar o que já existe a uma entrega de produção **com evidência**, sem nova capability de conversa. Os itens
vêm dos findings abertos, não de ideias novas:

| Candidato | Origem | Resultado esperado |
|---|---|---|
| Executar o teste de contrato contra um RelayPlane implantado e registrar a evidência | F-002 | tabela de [`RELEASE_EVIDENCE.md`](./RELEASE_EVIDENCE.md) preenchida e versionada |
| Ensaiar backup → restore → `migrate check` → `verify_integrity` e registrar tempos/resultado | F-003 | relatório de ensaio; RPO/RTO observados |
| Pipeline de CI no repositório que rode os gates do baseline (incl. PostgreSQL e `app.ops check`) | F-007 (não existe arquivo de CI no repositório) | gates reproduzíveis fora da máquina local |
| Definir o mínimo de evidência operacional de observabilidade (agregação de métricas entre processos / exportador OTel **se** o ambiente-alvo exigir) | F-005 | decisão explícita (ADR) antes de qualquer implementação |
| Entrypoint de produção que componha `Runtime` + `RelayPlaneSender` + `webhook_app` (hoje só `app.serve`, de terminal; o README manda o operador compor) | F-006 (`SUPERSEDED`) | **DECISÃO EM ABERTO:** o framework fornece uma composição/entrypoint de produção oficial, ou a composição pelo operador continua sendo o contrato suportado? Nenhuma opção escolhida; nada a implementar antes da decisão |
| Empacotamento/execução em ambiente-alvo (imagem, configuração, segredos) | **não há finding nem decisão**; só é candidato porque “entrega de produção” o implica | requer especificação antes de virar increment |

Cada item vira increment com o contrato de [`06_AGENT_HANDOFF`](./06_AGENT_HANDOFF.md) só depois de aprovado.

### 3. Evidência operacional

Executar o item 2 em ambiente real e observar SLOs/alertas existentes (`ops/slo.yaml`). Resultado: dados para decidir o item 4. Não há critério numérico definido; **definir é parte da decisão**.

### 4. Decisão sobre a próxima capability

Escolha explícita do Product Owner entre as candidatas abaixo ou outra. Nenhuma está priorizada.

## Capacidades candidatas (sem prioridade, sem decisão)

| Candidata | Estado | Material |
|---|---|---|
| Grupos, menções e allowlist de contatos | Desenho apenas; hoje grupos são reconhecidos e **ignorados**; depende de campos que o gateway ainda não entrega | [`FUTURE_GROUPS.md`](./FUTURE_GROUPS.md) |
| Visão (imagens) por agente, opt-in | Desenho apenas; hoje imagem é só nomeada ao agente | [`FUTURE_VISION.md`](./FUTURE_VISION.md) |

## Backlog técnico conhecido (não priorizado; débitos declarados em fases anteriores)

Fonte: `IMPLEMENTATION_STATUS.md` (seções “Débitos”). Reconferir contra o código antes de transformar em increment.

- Eval harness: sem LLM-judge e sem comando `record` na CLI (a biblioteca grava cassettes).
- Release: sem associação automática canal→agente e sem janela de observação automática do canary.
- Orçamento de LLM: avaliação com cache; sem rollup diário nem trilha de “cruzou 80%/100%”; mensagens durante período excedido (`degrade`) não são respondidas depois do reset.
- `compare_versions` só em PostgreSQL e não é gate automático do `ReleaseManager`.
- Mídia: um arquivo por argumento; só tools HTTP (MCP não recebe arquivo); conteúdo em base64 no JSON; sem áudio de saída; sem conversão de formato embutida.
- Retenção não cobre `trace_retention`/`memory_retention` (inexistentes hoje).

## Reconciliação com o roadmap legado (o que mudou de estado)

| Item no `ROADMAP.md` legado | Realidade verificada | Tratamento |
|---|---|---|
| Fases 1–10 como trabalho futuro/DoD | Implementadas (status `PASS`, suíte verde) | histórico |
| Fases 11 e 12 | Ausentes do `ROADMAP.md`; existem só em `IMPLEMENTATION_STATUS.md` (observabilidade; lacunas de conversa) | registrado como drift (F-004) |
| Fase 13 (serve/pacote/extras) | Implementada (`app.serve`, extras `[postgres]`, `[anthropic]`) | histórico |
| Fase 14 e 14.1 | Implementadas; 14.1 só no status | histórico |
| Fase 15 “Hoje só o áudio é lido… nenhuma tool recebe a mídia” | Falso desde 15b (INV-068); 15a e 15b implementadas | histórico; a afirmação obsoleta está marcada no `ROADMAP.md` |
| 15c Visão | Virou ideia futura | candidata acima |
| “Itens pendentes” da Fase 13 (`choose` multi-campo, fallback de execução…) | Tratados na Fase 14 conforme o status; não reauditados aqui | não listados como próximos passos |
| “OpenAPI importer” (Fase 7) | Implementado (`core/definitions/openapi_import.py`, teste em `test_tool_import.py`) | histórico |
| “Pack registry”, FAQ/RAG, vector memory, streaming | Não bloqueiam v1; sem evidência de implementação ou decisão | non-goals atuais ([`00_PROJECT`](./00_PROJECT.md)) |
