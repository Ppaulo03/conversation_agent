# Invariantes normativas — conversation_agent v4

Este documento é a referência curta para invariantes que devem permanecer verdadeiras independentemente de adapter, provider, tenant ou domínio.

Toda alteração arquitetural que possa violar uma invariante deve atualizar também o teste nomeado correspondente.

As seguintes regras são normativas para a v1:

```text
INV-001  core/engine nunca importam adapters ou SDKs externos.
INV-002  o LLM nunca fornece tenant_id, contact_id, conversation_id ou secrets.
INV-003  toda operação externa passa por Capability -> Binding -> PolicyGate -> ToolRunner.
INV-004  toda ação protegida possui action_id persistente e confirmation action-scoped.
INV-005  confirmação aceita e ToolInvocation(PREPARED) são persistidas atomicamente.
INV-006  ToolInvocation UNKNOWN nunca sofre retry cego.
INV-007  Outbound só é entregue a partir da Durable Outbox.
INV-008  business state pertence aos sistemas externos; o runtime guarda apenas estado conversacional.
INV-009  toda mutação de conversa valida conversation_epoch/fencing token atual.
INV-010  alteração de argumento protegido invalida confirmação anterior.
INV-011  perda do conversation lease impede mutação de conversa, mas não impede o executor ainda válido de registrar o fato externo no ledger usando execution_epoch.
INV-012  cancelamento/restart só é aplicado em fronteiras seguras de step.
INV-013  mensagens/resultados de tools são dados não confiáveis, nunca instruções privilegiadas.
INV-014  um turno reexecutado reutiliza seu journal persistido antes de chamar novamente LLM ou tool.
INV-015  uma ToolInvocation em FAILED terminal nunca volta a EXECUTING; nova tentativa semântica recebe nova identidade.
INV-016  transições do ledger após I/O externo são cercadas por execution_epoch; aplicação do resultado à conversa é cercada por conversation_epoch.
INV-017  replay do journal é fail-closed: (turn_id, step_index, step_type) identifica o step e request_hash divergente aborta o turno com alerta.
INV-018  cada turno possui turn_reference_time imutável persistido em INBOUND_AGGREGATED; replay nunca recalcula "agora".
INV-019  ownership HUMAN nunca produz resposta automática do bot.
INV-020  evento inbound duplicado nunca cria turno duplicado.
INV-021  retry automático de write/irreversible só é permitido com idempotência explícita e reutilização da mesma idempotency key.
INV-022  prompt de confirmação só é elegível após ACCEPTED pelo canal; QUEUED/SENDING/UNKNOWN não autorizam ação protegida.
```

O primeiro agente real deve ser implementado **sem Pack obrigatório**. O primeiro Pack só é extraído depois que existir comportamento reutilizável comprovado.

---

## Mapa INV → teste obrigatório

| Invariante | Teste mínimo |
|---|---|
| INV-001 | `test_architecture_core_engine_do_not_import_adapters` |
| INV-002 | `test_llm_cannot_set_trusted_context_ids_or_secrets` |
| INV-003 | `test_external_operation_always_crosses_policy_gate_and_tool_runner` |
| INV-004 | `test_protected_action_has_persistent_action_id_and_scoped_confirmation` |
| INV-005 | `C02_inside_confirm_prepare_tx`, `C03_after_confirm_prepare_commit` |
| INV-006 | `test_unknown_never_blind_retries` |
| INV-007 | `test_outbound_delivery_only_from_outbox` |
| INV-008 | `test_runtime_does_not_own_business_state` |
| INV-009 | `C09_worker_zombie` |
| INV-010 | `test_changed_protected_args_invalidate_confirmation` |
| INV-011 | `test_lost_conversation_lease_can_finalize_ledger_only`, `C14_lease_lost_after_external_success` |
| INV-012 | `C12_cancel_during_external_call` |
| INV-013 | `test_tool_and_user_content_never_raise_instruction_authority` |
| INV-014 | `C10_after_llm_response`, `C16_after_apply_before_compose` |
| INV-015 | `test_terminal_failed_invocation_requires_new_semantic_attempt` |
| INV-016 | `test_execution_epoch_and_conversation_epoch_have_disjoint_write_boundaries`, `C07_after_ledger_finalize`, `C14_lease_lost_after_external_success` |
| INV-017 | `test_journal_request_hash_divergence_fails_closed`, `C15_journal_hash_divergence` |
| INV-018 | `test_replay_reuses_turn_reference_time` |
| INV-019 | `test_human_owner_never_emits_automated_outbound` |
| INV-020 | `test_duplicate_event_never_creates_duplicate_turn` |
| INV-021 | `test_write_retry_requires_supported_idempotency_and_same_key` |
| INV-022 | `C13_confirm_while_prompt_ambiguous` |

Os casos `C01`–`C16` (ponto de falha, resultado esperado e fase em que passam a ser exigidos) estão definidos em [`ROADMAP.md`](./ROADMAP.md#chaos-gates).

## Regra de alteração

Uma PR que altera persistência, fencing, idempotência, confirmação, retry ou delivery protocol deve:

1. apontar as invariantes afetadas;
2. atualizar o teste/chaos case correspondente;
3. atualizar `RUNTIME_PROTOCOL.md`;
4. atualizar `ROADMAP.md` se o DoD de alguma fase mudar.
