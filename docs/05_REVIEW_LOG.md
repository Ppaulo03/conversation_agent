# 05 — Review log

O que está **tecnicamente em aberto**. Lifecycle e severidades da [metodologia §17–18](./01_METHODOLOGY.md):

- Status: `OPEN` · `PARTIALLY_RESOLVED` · `RESOLVED` · `ACCEPTED_RISK` · `SUPERSEDED`
- Severidade: `BLOCKER` · `HIGH` · `MEDIUM` · `LOW` · `NICE_TO_HAVE`

Regra de uso: toda review começa por este arquivo (evita “review amnesia”). Um finding só vira `RESOLVED` com a evidência da
resolução anexada; `ACCEPTED_RISK` exige decisão explícita do Product Owner registrada aqui.

## Baseline executado — 2026-10-07

Ambiente: Windows 11, Python 3.13.1 (`.venv` do projeto, sem `uv sync` nem alteração de dependências), PostgreSQL 16 em Docker
(`conversation_agent_postgres`, já em execução), base `a9c7af9` (`main`) mais somente arquivos `.md`. Logs brutos ficaram fora do repositório (scratchpad da sessão).

| Gate | Comando | Resultado | Evidência / notas |
|---|---|---|---|
| Lint | `python -m ruff check .` | **PASS** | `All checks passed!` |
| Formato | `python -m ruff format --check .` | **PASS** | `360 files already formatted` |
| Tipos (comando oficial do projeto) | `python -m mypy src examples` | **PASS** | `Success: no issues found in 218 source files` (o comando de `README.md`/`AGENTS.local.md`) |
| Tipos (`mypy .`, fora do gate) | `python -m mypy .` | **FAIL (informativo)** | 268 erros em 87 arquivos, todos em `tests/` (imports `support.*`/`conftest`/`postgres.world` não resolvidos pela configuração, anotações). Não é gate oficial (a configuração só declara `src` e `examples`); ver F-008 (RESOLVED) |
| Fronteiras | `lint-imports` | **PASS** | `Contracts: 9 kept, 0 broken.` |
| Suíte padrão (inclui PostgreSQL, chaos, invariantes, arquitetura, evals, contratos) | `python -m pytest -rs -q -p no:cacheprovider` | **PASS** | `1649 passed, 8 deselected in 414.30s`; zero skips. `tests/postgres` (435 testes) rodaram contra o PostgreSQL real; `tests/architecture` (229) inclui `test_chaos_gates.py` (C01–C16 exigidos) |
| Artefatos operacionais | `python -m conversation_agent.app.ops check` | **PASS** | `ok` (ops/ em dia com `ops/slo.yaml`) |
| Contrato RelayPlane real | `pytest tests/contracts/test_relayplane_live.py -m integration -rs` | **NOT_VERIFIED** | 3 skipped: variáveis `RELAYPLANE_*` ausentes do ambiente. Ver F-002 |
| Testes live com LLM | `pytest -m integration` (5 testes em `tests/integration/`) | **NOT_VERIFIED (opt-in)** | Não executados de propósito: exigem credenciais, consomem o orçamento diário do provedor e enviam dados a um serviço externo. Não condicionam o baseline do repositório |
| Migrations contra um banco de ambiente | `python -m conversation_agent.app.migrate check` | **NOT_VERIFIED (ambiente)** | `DATABASE_URL` não definido; o comportamento do `Migrator` é coberto por `tests/postgres/test_migrator.py` (PASS), mas o banco de dev não foi checado |
| Restore/DR | — | **NOT_VERIFIED** | Não existe comando nem teste de restore; ver F-003 |
| CI do repositório | — | **NOT_VERIFIED** | Não existe arquivo de pipeline no repositório; ver F-007 |

Os seis primeiros gates `PASS` constituem o **baseline do repositório** (F-001). Os `NOT_VERIFIED` são evidência externa/operacional e estão rastreados em F-002, F-003, F-005 e F-007; `migrate check` de um deployment concreto e os testes live com LLM pertencem à Production Delivery / opt-in. `PASS` não prova o contrato do gateway, restore nem operação em produção.

## Resumo

| ID | Severidade | Status | Título |
|---|---|---|---|
| F-001 | MEDIUM | RESOLVED | Baseline verificável do repositório |
| F-002 | HIGH | OPEN | Evidência do contrato RelayPlane real |
| F-003 | HIGH | OPEN | Ensaio de disaster recovery |
| F-004 | MEDIUM | RESOLVED | Deriva de autoridade da documentação |
| F-005 | MEDIUM | OPEN | Lacunas de observabilidade operacional |
| F-006 | MEDIUM | SUPERSEDED | Entrypoint de produção com RelayPlane (virou decisão em aberto no roadmap) |
| F-007 | MEDIUM | OPEN | Sem pipeline de CI no repositório |
| F-008 | LOW | RESOLVED | `mypy .` fora do escopo configurado |

Contagem: RESOLVED 3 (F-001, F-004, F-008) · OPEN 4 (F-002, F-003, F-005, F-007) · SUPERSEDED 1 (F-006) · PARTIALLY_RESOLVED 0 · ACCEPTED_RISK 0.
Não há `BLOCKER`. Os `HIGH` são F-002 e F-003; são findings de Production Delivery, não da consolidação, e ficam `OPEN` para a próxima iteration.

---

### F-001 — Baseline verificável do repositório
- **Severity:** MEDIUM
- **Status:** RESOLVED (2026-10-07, base `a9c7af9`)
- **Evidence:** `ruff check .` PASS; `ruff format --check .` PASS (360 arquivos); `mypy src examples` PASS (218 arquivos); `lint-imports` PASS (9 contratos); `pytest` PASS (1649 passed, 8 deselected, 0 skips, 414 s; inclui PostgreSQL real, chaos C01–C16, invariantes, arquitetura, evals); `app.ops check` PASS. Detalhe na tabela do baseline.
- **Consequence:** o código do repositório tem baseline explícito e reproduzível; aceitar a iteration não depende de evidência de produção.
- **Scope note:** F-001 cobre só o baseline do repositório. Itens externos ficam em seus próprios findings: RelayPlane real → F-002; restore/DR → F-003; observabilidade operacional → F-005; CI → F-007. Testes live com LLM são opt-in (`NOT_VERIFIED`) e `migrate check` de um deployment concreto é evidência de ambiente (Production Delivery); nenhum dos dois condiciona o baseline.
- **Expected resolution:** repetir a tabela (data + commit) a cada iteration que mude o código.

### F-002 — RelayPlane live contract evidence
- **Severity:** HIGH
- **Status:** OPEN
- **Evidence:** `docs/RELEASE_EVIDENCE.md` tem a tabela de registro em branco; `tests/contracts/test_relayplane_live.py` (3 testes) foi `skipped` nesta execução por falta de `RELAYPLANE_*`; `IMPLEMENTATION_STATUS.md` marca o achado 5 da auditoria externa e o 6 da Fase 14.1 como “pendente (você)”; `RELAYPLANE_CONTRACT.md` foi lido no commit `880820a` do repositório do gateway, sem registro de reconferência. Pista não verificada: há um container local `relayplane/evolution:patched-test`; não está registrado que o teste tenha rodado contra ele.
- **Consequence:** INV-021/034/061/066 e ADR-005/008 dependem de duas premissas do gateway (mesma `Idempotency-Key` dentro da retenção devolve a mesma mensagem; `/limits` reporta a retenção real). Sem evidência, um gateway que não deduplicasse geraria mensagens duplicadas, e o desenho assume o contrário.
- **Expected resolution:** executar o roteiro de `RELEASE_EVIDENCE.md` contra o gateway de produção (seções 1 e 2), preencher e versionar a tabela com commit e versão do gateway; repetir a cada mudança de versão/retenção do gateway.

### F-003 — Disaster recovery rehearsal
- **Severity:** HIGH
- **Status:** OPEN
- **Evidence:** `docs/OPERATIONS.md` (“Backup and disaster recovery”) define o procedimento e diz “Rehearse this. A backup that was never restored is a hypothesis”; a busca por `pg_dump|pg_restore|pg_basebackup` em código, testes e docs não encontra nada além dessa prosa; `IMPLEMENTATION_STATUS.md` (débitos da Fase 10): “Não há rehearsal automatizado de restore no CI”; RPO/RTO não têm valores (“decida por ambiente”). Existe e é testado `verify_integrity` (`tests/postgres/test_ops.py`), mas só prova a checagem, não um restore.
- **Consequence:** o único estado durável é o PostgreSQL e as garantias de reenvio dependem da janela de idempotência do gateway; um restore nunca ensaiado pode revelar perda além da janela só durante um incidente.
- **Expected resolution:** ensaio documentado (backup → restore em ambiente separado → `migrate check` → `verify_integrity` → workers retomam), com tempo medido, RPO/RTO observados, e comparação do ponto de restauração com `idempotency_retention_seconds`; registrar o resultado aqui e em `OPERATIONS.md`.

### F-004 — Documentation authority drift
- **Severity:** MEDIUM
- **Status:** RESOLVED (2026-10-07)
- **Evidence (divergências encontradas, todas só documentais):**
  1. `ROADMAP.md` se declarava “Fonte única das fases” mas não contém as Fases 11, 12 nem 14.1/15a/15b, que existem em `IMPLEMENTATION_STATUS.md` e no código.
  2. Três regras de precedência incompatíveis: `IMPLEMENTATION_STATUS.md` (INVARIANTS > RUNTIME_PROTOCOL > DESIGN > ROADMAP), `DESIGN.md` §52 (INVARIANTS/RUNTIME_PROTOCOL/ROADMAP prevalecem sobre o DESIGN) e `docs/README.md`.
  3. `ROADMAP.md` (Fase 15): “nenhuma tool recebe a mídia” — falso desde a 15b (INV-068).
  4. `DESIGN.md` §1.4: “INV-001 a INV-022” — o conjunto vai até INV-071.
  5. `IMPLEMENTATION_STATUS.md`: cabeçalho “Fase 13 — Uso real (em andamento)” enquanto o ROADMAP e o corpo a dão por concluída; “Fase atual: 15”.
  6. `docs/README.md` não conhecia a hierarquia `00`–`06`.
  7. `RUNTIME_PROTOCOL.md` §8 dizia que o RelayPlane não define janela outbound formal; o contrato publicado (`RELAYPLANE_CONTRACT.md`) a expõe em `GET /limits`.
- **Resolution:** ADR-020 fixa a hierarquia; cada documento atual tem dono claro (`00`–`06`, `INVARIANTS`, `RUNTIME_PROTOCOL`); `ROADMAP.md`, `DESIGN.md` e `IMPLEMENTATION_STATUS.md` estão marcados como históricos; `README.md`, `docs/README.md` e `AGENTS.local.md` apontam para a memória canônica; as afirmações 1–7 foram corrigidas ou marcadas (nenhum texto histórico apagado; `RUNTIME_PROTOCOL.md` §8 foi reescrito para o contrato vigente).
- **Policy:** `DESIGN.md` permanece preservado como racional histórico e não é fonte do estado atual. Não há auditoria preventiva linha a linha; divergências futuras encontradas durante trabalho real geram findings específicos.

### F-005 — Operational observability gaps
- **Severity:** MEDIUM
- **Status:** OPEN
- **Evidence:** (a) não há exportador OpenTelemetry: `core/tracing.py` é a interface, `pyproject.toml` não depende de OpenTelemetry (ADR-010); (b) métricas de caminho quente e de tools/circuito são em memória por processo, sem agregação nem push (status, débitos das fases 10–11); (c) orçamento de LLM avaliado com cache e sem rollup diário (fase 11); (d) a taxa de confirmações “não provadas” só existe em log, sem métrica (fase 13); (e) o envio ao RelayPlane não propaga `trace_id` (contrato do gateway não define); (f) `ops/alerts.rules.yml` e o dashboard são verificados apenas por consistência com `ops/slo.yaml` (`app.ops check`): nenhum teste ou documento registra tê-los carregado em um Prometheus/Grafana reais.
- **Consequence:** existe observabilidade suficiente para diagnosticar com logs, `trace_id` e `/metrics` por instância, mas **não há evidência de que os alertas disparam em operação real** nem visão agregada com mais de um processo.
- **Expected resolution:** definir (ADR) o mínimo de evidência operacional para a entrega de produção; validar regras/dashboard num stack real; decidir sobre exportador OTel/agregação **apenas se** esse mínimo exigir.

### F-006 — Entrypoint de produção com RelayPlane
- **Severity:** MEDIUM
- **Status:** SUPERSEDED (por uma decisão em aberto em [`04_ROADMAP.md`](./04_ROADMAP.md), seção Production Delivery)
- **Evidence (preservada):** `RelayPlaneSender` e `webhook_app` são referenciados em `src/` somente nos próprios adapters; fora deles, só testes os compõem. `app/serve.py` é o canal de terminal/desenvolvimento (débito declarado na Fase 13); o README instrui o operador a compor `Runtime.build(...)`.
- **Reclassification:** a ausência não é comprovadamente um defeito. A pergunta é de escopo: o framework deve fornecer uma composição/entrypoint de produção oficial, ou a composição pelo operador continua sendo o contrato suportado? **Decisão explicitamente aberta; nenhuma opção foi escolhida e nenhuma implementação deve ocorrer antes dela.**

### F-007 — No CI pipeline in the repository
- **Severity:** MEDIUM
- **Status:** OPEN
- **Evidence:** `git ls-files` não contém `.github/`, `.gitlab-ci.yml` nem equivalente; a documentação cita “`app.ops check` roda no CI” (status Fase 10) e `ops check` foi desenhado para rodar sem asyncpg “no CI”.
- **Consequence:** os gates só existem como comandos manuais; nada prova que um push passa neles fora da máquina local, e a regra global de monitorar o CI do push não tem o que monitorar.
- **Expected resolution:** definir o provedor/pipeline e codificar os gates oficiais (incl. serviço PostgreSQL); confirmar a primeira execução verde.

### F-008 — `mypy .` fora do escopo configurado
- **Severity:** LOW
- **Status:** RESOLVED (2026-10-07)
- **Evidence:** `mypy .` → 268 erros em 87 arquivos de `tests/` (imports `support.*`/`conftest`/`postgres.world` não resolvidos pela configuração etc.); `mypy src examples` passa. `[tool.mypy]` declara `src` e os exemplos, não `tests/`.
- **Resolution:** o gate oficial é `mypy src examples` (já documentado em `README.md` e `AGENTS.local.md`; explicitado em `06_AGENT_HANDOFF.md`). `mypy .` não é gate. Tipar `tests/` seria decisão/increment separado e não foi feito.

---

## Histórico de resoluções

- 2026-10-07 (base `a9c7af9`): F-001, F-004, F-008 `RESOLVED`; F-006 `SUPERSEDED`. Evidência nas respectivas seções.

Ao resolver um finding, registre: ID, data, commit e a evidência (comando + resultado).
