# 06 — Agent handoff

Contrato padrão para delegar **um increment** a um coding agent ([metodologia §9–§12, §42–§43](./01_METHODOLOGY.md)).
Quem delega preenche só as seções que melhoram o contrato; o resto sai. Envie o menor contexto suficiente: o increment, as
invariantes e decisões relevantes (por ID) e as áreas afetadas — não este repositório de documentação inteiro.

## Regras permanentes para o agente

```text
Implement ONLY this increment.
Preserve behavior accepted by previous increments.
Do not implement future roadmap items.
Do not introduce a new architectural mechanism unless required by the specification.
If an architectural decision is required and is not covered by the specification,
report it instead of silently making that decision.
```

Além disso, para este repositório:

1. Leia `AGENTS.local.md` (comandos, aprendizados) e, do contexto canônico, só o que o increment cita:
   [`02_ARCHITECTURE`](./02_ARCHITECTURE.md), ADRs em [`03_DECISIONS`](./03_DECISIONS.md), invariantes em [`INVARIANTS.md`](./INVARIANTS.md), findings em [`05_REVIEW_LOG`](./05_REVIEW_LOG.md).
2. Inspecione antes de alterar. Quando documentação e código divergirem, o código e os testes são a realidade: registre a divergência, não a esconda.
3. Mudança de comportamento exige teste; mudança de protocolo exige atualizar invariante, `RUNTIME_PROTOCOL.md` e o teste nomeado correspondente.
4. Sem refactors oportunistas, abstrações novas ou superfícies de configuração novas. Ideia útil fora do escopo ⇒ `FOLLOW-UP CANDIDATE` no relatório.
5. Rode e informe cada gate com `PASS | FAIL | NOT_VERIFIED` (comando + causa). Gate não executado nunca é `PASS`.
6. Git: branch `feature/<ticket>-<descricao>` ou `fix/…`; nunca em `main`; Conventional Commits em inglês; um commit lógico por vez; sem `git add -A` às cegas; sem segredos; sem push/merge/deploy/migração sem aprovação explícita.
7. Nunca ler nem imprimir `.env`.

## Template

```markdown
# Increment <id> — <título curto>

## Objective
<uma frase: o que este increment realiza>

## Context
<por que é necessário; referencie ADR-XXX / INV-XXX / F-XXX em vez de copiar texto>

## Initial state
<o que já existe e deve ser preservado; commit/base; testes verdes de partida>

## Scope
<o que muda, de forma observável>

## Out of scope
<o que NÃO pode mudar agora (inclua capacidades futuras próximas que o agente poderia antecipar)>

## Invariants
<invariantes/ADRs que continuam verdadeiros; ex.: INV-005, INV-021; fronteiras import-linter>

## Expected behavior
<comportamento após a mudança, como afirmações de comportamento, não de implementação>

## Acceptance criteria
- [ ] <critério objetivamente verificável 1>
- [ ] <critério 2>
- [ ] Gates: `ruff check .`, `ruff format --check .`, `mypy src examples` (o gate de tipos oficial; `mypy .` NÃO é gate), `lint-imports`, `pytest` (incl. PostgreSQL) com resultado reportado

## Risks
<o que pode dar errado facilmente: concorrência, digest de agente, migração, compatibilidade>

## Dependencies
<increments/decisões/findings dos quais depende; o que precisa estar aceito antes>

## Likely affected areas
<módulos/contratos esperados; arquivos fora desta lista exigem justificativa no relatório>
```

## Formato do relatório do agente

```text
Files changed:        <lista>
Behavioral changes:   <NONE | descrição e justificativa>
Gates:                <Gate / Command / Result PASS|FAIL|NOT_VERIFIED / Evidence / Notes>
Assumptions:          <...>
Follow-up candidates: <...>
Findings:             <novos ou atualizados, com ID, severidade e status do lifecycle>
```

Se uma decisão arquitetural for necessária e não estiver coberta:

```text
ARCHITECTURAL DECISION REQUIRED

Context:
...

Decision required:
...

Why the current specification is insufficient:
...

Possible options:
...
```

Trabalho não relacionado dentro do increment pode prosseguir se for seguro; o ponto de decisão fica pendente.

## Critério de aceite do Engineering Copilot

Um increment é aceito quando ([metodologia §21, §24](./01_METHODOLOGY.md)): objetivo atendido, critérios cumpridos, nenhum `BLOCKER`
aberto, nenhum `HIGH` diretamente relacionado aberto sem `ACCEPTED_RISK`, testes relevantes passando, documentação canônica atualizada
(`02`/`03`/`05` quando aplicável) e nenhuma decisão arquitetural embutida em silêncio.
