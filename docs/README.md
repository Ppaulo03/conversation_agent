# conversation_agent — documentação

## Memória canônica (comece aqui)

Ordem de autoridade e papel de cada documento: [ADR-020](./03_DECISIONS.md).

- [`00_PROJECT.md`](./00_PROJECT.md): o que construímos, objetivos, non-goals, estado atual.
- [`01_METHODOLOGY.md`](./01_METHODOLOGY.md): como trabalhamos (metodologia oficial adotada).
- [`02_ARCHITECTURE.md`](./02_ARCHITECTURE.md): como o sistema funciona agora.
- [`03_DECISIONS.md`](./03_DECISIONS.md): por que as decisões duráveis foram tomadas (ADRs).
- [`04_ROADMAP.md`](./04_ROADMAP.md): para onde vamos.
- [`05_REVIEW_LOG.md`](./05_REVIEW_LOG.md): findings técnicos abertos e baseline executado.
- [`06_AGENT_HANDOFF.md`](./06_AGENT_HANDOFF.md): contrato para delegar increments a coding agents.

## Normativo e vigente

- [`INVARIANTS.md`](./INVARIANTS.md) e [`RUNTIME_PROTOCOL.md`](./RUNTIME_PROTOCOL.md).
- Operação e uso: [`OPERATIONS.md`](./OPERATIONS.md), [`GUIDE.md`](./GUIDE.md), [`RELAYPLANE_CONTRACT.md`](./RELAYPLANE_CONTRACT.md), [`RELEASE_EVIDENCE.md`](./RELEASE_EVIDENCE.md).

## Histórico (preservado, sem autoridade sobre o estado atual)

- [`DESIGN.md`](./DESIGN.md) (racional de desenho), [`ROADMAP.md`](./ROADMAP.md) (fases originais; só a tabela de chaos gates segue viva),
  [`../IMPLEMENTATION_STATUS.md`](../IMPLEMENTATION_STATUS.md) (log de implementação por fase).
- Futuro, não implementado: [`FUTURE_VISION.md`](./FUTURE_VISION.md), [`FUTURE_GROUPS.md`](./FUTURE_GROUPS.md).

---

## Índice original (v4, preservado)

Arquivos:

- [`FUTURE_VISION.md`](./FUTURE_VISION.md): visão (imagens) como evolução futura (FUTURO, não implementado).

- [`FUTURE_GROUPS.md`](./FUTURE_GROUPS.md): grupos, menções e allowlist de contatos (FUTURO, não implementado).

- [`GUIDE.md`](./GUIDE.md): guia de uso (em inglês, como o README e o código): do manifest ao runtime, o que o canal precisa fornecer. Os demais documentos são de desenho e ficam em português.

- [`DESIGN.md`](./DESIGN.md): visão consolidada e racional do desenho (em divergência, prevalecem `INVARIANTS.md` e `RUNTIME_PROTOCOL.md`).
- [`INVARIANTS.md`](./INVARIANTS.md): invariantes normativas e mapa para testes.
- [`RUNTIME_PROTOCOL.md`](./RUNTIME_PROTOCOL.md): protocolo de lease, claim, epochs, journal, tools, confirmation e Outbox.
- [`ROADMAP.md`](./ROADMAP.md): fases, DoD, GO/NO-GO e chaos gates (histórico; vigente: `04_ROADMAP.md`).

Regra de manutenção: uma mudança que altera protocolo deve atualizar a invariante, o runtime protocol e o teste correspondente.
