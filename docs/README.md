# conversation_agent — documentação v4

Arquivos:

- [`DESIGN.md`](./DESIGN.md): visão consolidada e racional do desenho (em divergência, prevalecem os três documentos abaixo).
- [`INVARIANTS.md`](./INVARIANTS.md): invariantes normativas e mapa para testes.
- [`RUNTIME_PROTOCOL.md`](./RUNTIME_PROTOCOL.md): protocolo de lease, claim, epochs, journal, tools, confirmation e Outbox.
- [`ROADMAP.md`](./ROADMAP.md): fases, DoD, GO/NO-GO e chaos gates.

Regra de manutenção: uma mudança que altera protocolo deve atualizar a invariante, o runtime protocol e o teste correspondente.
