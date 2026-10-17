# 04 — Roadmap (para onde vamos)

Só trabalho futuro e a direção atual. **Não é histórico**: o que já foi implementado está em
[`IMPLEMENTATION_STATUS.md`](../IMPLEMENTATION_STATUS.md) (log) e descrito em [`02_ARCHITECTURE`](./02_ARCHITECTURE.md). O
[`ROADMAP.md`](./ROADMAP.md) legado é histórico de fases (mantém só a tabela de chaos gates como referência viva, ver ADR-020).
Itens abaixo são **direção recomendada**, não compromissos de escopo, até decisão explícita do Product Owner.

## Direção atual

```text
Baseline & Engineering Consolidation   (aceita — sem mudança de comportamento)
    ↓
Framework Integration Readiness
    ↓
adapter / integration evidence
    ↓
operational contract completeness
    ↓
decisão explícita sobre a próxima capability
```

### 1. Baseline & Engineering Consolidation — aceita

Entregue: baseline do repositório verificável (F-001 `RESOLVED`), memória canônica `00`–`06`, autoridade documental reconciliada
(F-004 `RESOLVED`). Dos findings de evidência, F-002 e F-007 seguem `OPEN` e alimentam a próxima iteration; F-003, F-005 e F-006 foram reclassificados pela ADR-022 (o framework é embeddable e não possui deployment runtime canônico).

### 2. Framework Integration Readiness — próxima iteration (candidata, aguardando decisão de escopo)

Objetivo: verificar que o framework pode ser integrado com segurança por uma aplicação consumidora (ADR-022), **sem criar
deployment, entrypoint, imagem ou container padrão** e sem nova capability de conversa. Os itens vêm dos findings abertos:

| Candidato | Origem | Resultado esperado |
|---|---|---|
| Executar o teste de contrato contra um RelayPlane implantado e registrar a evidência (o adapter é do framework) | F-002 | tabela de [`RELEASE_EVIDENCE.md`](./RELEASE_EVIDENCE.md) preenchida e versionada |
| Pipeline de CI **do próprio framework** que rode os gates do baseline (incl. PostgreSQL e `app.ops check`); CI/CD do consumidor está fora de escopo | F-007 | gates reproduzíveis fora da máquina local |
| Avaliar a Integration Readiness: o consumidor tem o que precisa (composition APIs, ports, contratos operacionais, exemplo de integração) sem reconstruir contratos implícitos | ADR-022 | lista de lacunas concretas, cada uma virando finding próprio |

Cada item vira increment com o contrato de [`06_AGENT_HANDOFF`](./06_AGENT_HANDOFF.md) só depois de aprovado. Decidido por ADR-022
(portanto **fora** do roadmap): entrypoint/ASGI host/imagem/deployment oficial, backup/restore operacional, RPO/RTO e backend
de métricas/tracing.

### 3. Adapter / integration evidence e completude do contrato operacional

Executar o item 2 e fechar as lacunas do contrato operacional que o framework oferece ao consumidor (`OPERATIONS.md`, *Framework contract*). Resultado: dados para decidir o item 4. Não há critério numérico definido; **definir é parte da decisão**.

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
