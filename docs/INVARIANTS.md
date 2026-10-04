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
INV-023  uma ToolInvocation PREPARED é imutável quanto à operação externa que representa: retry e reconciliation executam a intent congelada no PREPARE e nunca re-resolvem binding/tool com outra versão; se a operação resolvida mudou, falham fechado e escalam.
INV-024  um lease de conversa expirado não autoriza escrita, mesmo antes de qualquer takeover; o heartbeat nunca ressuscita um lease expirado.
INV-025  aguardar o resultado de uma tool (EXECUTING/UNKNOWN/RECONCILING) não é falha do turno e nunca consome tentativas nem dead-letteriza o inbound.
INV-026  provider_accepted_at é o horário de aceite no domínio do canal ou NULL; nunca o relógio local/do banco. Sem timestamp comparável nem reply-to, a resposta é ambígua e o runtime pergunta de novo.
INV-027  uma operação PREPARED não muda de destino: a conexão resolvida (fingerprint sem segredos) e, em status_lookup, o próprio lookup (args + destino) são congelados no PREPARE; mudança de destino/lookup falha fechado (nada é enviado ou decidido).
INV-028  uma versão publicada de agente é imutável (mesmo conteúdo = no-op, outro conteúdo na mesma versão = erro; versões só avançam; mudança incompatível exige bump MAJOR, avaliada sobre o risco/proteção EFETIVOS). Uma conversa fica fixada em (agent_id, versão) enquanto há algo em andamento (outro agente com trabalho aberto falha fechado) e uma invocação é reconciliada com a versão que a preparou; versão ausente falha fechado (nunca outra versão).
INV-029  um agente só roda depois de compilado (o runtime só aceita CompiledAgent, que só o compiler produz): referências, risco, mapeamentos, schemas, error_map e versões inválidos são rejeitados antes do runtime, e o conteúdo carregado deve bater com o digest publicado.
INV-030  o grafo de runtime tem exatamente um CompiledAgent como raiz de definição: engine, pipeline, estágio de confirmação e reconciliation derivam do mesmo agente compilado (versão + digest conferidos); uma protected action nunca existe sem a pergunta de confirmação do runtime (`confirmation_prompt_enabled=false` é rejeitado quando há capability protegida; os templates de confirmação precisam conter `{summary}`).
INV-031  o registry de agentes é tenant-scoped: `(tenant_id, agent_id, version)` é a identidade; um tenant nunca lê nem sombreia agentes de outro.
INV-032  nenhuma comparação de lease/claim/backoff mistura o relógio do worker com a autoridade de coordenação: o default de todo adapter PostgreSQL é o relógio do banco (`clock_timestamp()`); o `Clock` da aplicação só governa tempo conversacional. A checagem síncrona de lease estima o tempo da autoridade (leitura do banco + tempo monotônico decorrido).
INV-033  uma credencial nunca sobrescreve um header do runtime (`Idempotency-Key`, `Host`, `X-*` de identidade); e `base_url` é só scheme+host+porta+path (sem userinfo, query ou fragment), de modo que o destino congelado cobre tudo que define a URL.
INV-034  o sender só reenvia a mesma linha da outbox (mesma `Idempotency-Key`) enquanto o canal ainda lembra a chave: `sender_retry_horizon <= retenção` (validado); passado o horizonte a linha vira UNKNOWN e é reconciliada por consulta ao canal. Um `404` só prova ausência dentro da janela; fora dela (ou com erro no lookup) a linha continua UNKNOWN, nunca reenvio cego.
INV-035  um webhook só é reconhecido (2xx) depois de a assinatura HMAC (com janela anti-replay) ser verificada e o evento persistido no inbox; tenant e canal vêm do cadastro da subscription, nunca do payload; falha de persistência é 5xx.
INV-036  mídia entra como referência (claim-check), nunca como bytes no estado/journal; a transcrição é um step journalado (replay não retranscreve) e uma falha dela nunca falha o turno. Mensagem proativa passa pelo mesmo runtime (evento de sistema deduplicado pela chave do timer), só sai se a conversa for do bot e a política de canal permitir (sem política configurada: nada sai), e nunca estende a janela de atendimento do contato. Mudança de ownership só ocorre sob o lease da conversa.
INV-037  descoberta de tools não é exposição: só vira Tool Definition o que uma allowlist explícita nomeia, com risco decidido pelo operador (dica do servidor só pode aumentar o cuidado); o schema de entrada do servidor é pinado por digest e uma chamada nunca é enviada se o schema atual divergir do pinado ou a tool sumiu; o que o servidor devolve (dados, erro, descrição) é dado não confiável e nunca instrução.
INV-038  um circuit breaker só falha rápido ANTES de chamar o provider (resultado `technical_error` retryable = não enviado, seguro até para escrita) e nunca reclassifica o que o provider respondeu; só saúde do destino conta contra o circuito.
INV-039  o replay de tools não tem transporte: chamada não gravada é `REPLAY_MISS` explícito, nunca uma resposta inventada; a gravação redige PII por padrão.
INV-040  um Pack só contribui definições (capabilities, flows, fragmentos de prompt, requisitos, evals): não carrega tools, bindings, connections nem secrets, não amplia o que o modelo pode chamar (só o `allow` explícito do agente), nunca sombreia nem sobrescreve uma definição do agente ou de outro Pack, e o que exige do agente (binding, idempotência, recuperação) é verificado na instalação. Depois da compilação o agente é autocontido: o runtime nunca depende do Pack e a procedência fica em `pack_lock`.
INV-041  uma preferência declarada pelo contato (horário/data) é casada contra TODAS as opções reais que a tool devolveu, não só as que couberam na mensagem; um número ou ordinal refere-se apenas ao que foi mostrado.
INV-042  quando um Flow diz que vai chamar uma pessoa (`Handoff`), a conversa realmente passa a HANDOFF_PENDING, também quando o handoff vem depois da execução de uma ação protegida (caminho da confirmação), na mesma transação que grava a resposta.
INV-043  toda ação administrativa (publicar agente, decisão de release, mudança de ownership por operador, política/execução de retenção, erasure) deixa uma trilha append-only (imposta pelo banco) com o ator; nas operações PostgreSQL próprias a trilha é gravada na MESMA transação da ação; a trilha guarda identificadores e contagens, nunca conteúdo nem o identificador da pessoa.
INV-044  um release nunca migra uma conversa com trabalho em andamento; promover ou abrir canary exige evidência de eval aprovada para o digest exato do agente; uma versão retirada (rollback) não volta a ser liberada.
INV-045  apagar um contato remove ou pseudonimiza tudo que o nomeia (nenhuma tabela, nenhuma coluna), mantém como tombstone sem conteúdo as linhas que dedupe/idempotência/ledger exigem, e recusa enquanto houver turno, lease, efeito externo inacabado, mensagem a entregar ou confirmação pendente (`force` só cancela mensagens e confirmações).
INV-046  backpressure recusa ANTES de persistir ou reconhecer (o gateway reentrega: atraso, nunca perda) e só atinge mensagens novas do usuário; o limite de chamadas de tool recusa antes de enviar, de modo que uma escrita negada é não-execução conhecida (`technical_error` retryable), nunca `unknown`.
INV-047  o schema só avança (forward-only, checksum por migration): uma migration aplicada que mudou, ou um banco à frente do código, impede aplicar e iniciar; migrators concorrentes são serializados e cada migration é atômica.
INV-048  observabilidade é canal lateral: falha de logger, tracer, métrica ou do ledger de uso nunca falha, atrasa semanticamente nem altera o resultado do trabalho observado (o que o provider/tool devolve passa inalterado); a falha é contada e alertada.
INV-049  nenhum dado de conteúdo ou pessoa chega a logs, spans, métricas ou ao ledger de uso: o contexto é um conjunto FECHADO de campos, texto livre é redigido e limitado, exceções saem como tipo + mensagem redigida, e a conversa é identificada só por referência não reversível.
INV-050  o gasto de LLM é registrado uma vez por chamada REAL ao provider (um replay do journal nunca conta de novo; uma chamada repetida após crash conta de novo, pois foi cobrada), toda chamada tem dono (sem contexto: `_unattributed`), e um modelo sem preço é reportado como não precificado, nunca como gratuito; preços são dado aplicado na leitura, ao preço do dia da chamada.
INV-051  um orçamento de LLM só recusa se a política do tenant pedir (`refuse_new`), recusa apenas mensagens NOVAS do usuário e antes de persistir ou reconhecer qualquer coisa; o padrão é alertar.
INV-052  um pedido de atendente humano (agente com `human_request`) é decidido por regra, antes de qualquer Flow ou modelo: mensagem curta, sem negação; com atendimento disponível a conversa vai a HANDOFF_PENDING na mesma transação da resposta e o bot silencia; sem atendimento disponível (`available: false`) diz a resposta fixa e NÃO muda o dono da conversa.
INV-053  um slot de texto livre com `question_check: model` nunca toma como valor uma mensagem com cara de pergunta sem que o modelo (journalado) decida que é a resposta; sem o opt-in o comportamento e o digest do agente são os de sempre.
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
| INV-023 | `test_prepared_invocation_executes_the_frozen_intent_never_a_re_resolved_one`, `test_a_deploy_between_prepare_and_execute_fails_closed_without_sending` |
| INV-024 | `test_an_expired_lease_cannot_open_a_unit_of_work_even_before_any_takeover`, `test_a_heartbeat_cannot_resurrect_an_expired_lease` |
| INV-025 | `test_pending_tool_result_never_exhausts_turn_attempts` |
| INV-026 | `test_a_channel_that_gives_no_timestamp_leaves_accepted_at_null_not_local_time`, `test_a_legitimate_reply_is_eligible_when_the_channel_clock_runs_behind` |
| INV-027 | `test_a_prepared_write_is_never_sent_to_a_destination_it_was_not_prepared_for`, `test_a_changed_lookup_binding_refuses_to_decide_absence`, `test_the_execution_path_refuses_a_stale_destination_without_sending` |
| INV-028 | `test_a_published_version_is_immutable`, `test_published_rows_are_immutable_in_the_database_itself`, `test_a_conversation_stays_on_its_version_while_something_is_in_progress`, `test_an_unknown_write_is_reconciled_with_the_version_that_prepared_it`, `test_reconciliation_never_substitutes_another_version`, `test_a_minor_version_cannot_lower_the_effective_risk`, `test_a_minor_version_cannot_remove_effective_protection`, `test_a_conversation_is_pinned_to_the_agent_not_just_the_version` |
| INV-029 | `tests/compiler/test_compiler.py` (refs, schemas, risco, bindings, versões), `test_a_stored_agent_that_no_longer_matches_its_digest_is_refused`, `test_only_the_compiler_can_produce_a_compiled_agent`, `test_a_mapping_cannot_feed_a_value_of_the_wrong_type` |
| INV-030 | `test_the_engine_refuses_a_pipeline_built_from_another_compiled_agent`, `test_a_protected_agent_cannot_turn_the_confirmation_question_off`, `test_confirmation_texts_must_be_formattable_and_say_what_is_confirmed` |
| INV-031 | `test_the_registry_is_tenant_scoped` |
| INV-032 | `test_stores_default_to_the_database_clock_not_the_workers_clock`, `test_a_workers_skewed_wall_clock_does_not_make_its_lease_look_expired`, `test_a_lease_expires_by_elapsed_authority_time_not_by_wall_clock`, `test_reconciliation_backoff_is_a_coordination_timestamp` |
| INV-033 | `test_auth_cannot_name_a_runtime_owned_header`, `test_even_a_forged_auth_spec_never_overwrites_the_idempotency_key`, `test_base_url_is_only_scheme_host_port_and_path` |
| INV-034 | `test_the_sender_does_not_resend_blindly_past_the_retry_horizon`, `test_past_the_gateways_retention_no_resend_is_safe_and_nothing_is_resent`, `test_a_lost_response_is_resent_with_the_same_key_and_never_duplicates`, `test_a_gateway_unknown_waits_for_a_decision_and_is_never_guessed`, `test_the_retry_horizon_must_fit_inside_the_gateways_idempotency_retention` |
| INV-035 | `test_a_signed_message_is_persisted_before_the_2xx`, `test_unverifiable_requests_are_rejected_and_never_persisted`, `test_the_payload_cannot_choose_the_tenant`, `test_the_event_id_header_must_agree_with_the_signed_body`, `test_nothing_is_acknowledged_that_was_not_persisted` |
| INV-036 | `test_a_replayed_turn_never_transcribes_again`, `test_media_references_cannot_smuggle_bytes_or_unbounded_fields`, `test_a_timer_survives_a_restart_and_sends_exactly_one_message`, `test_a_human_owned_conversation_stays_silent_for_timers`, `test_without_a_channel_policy_nothing_proactive_is_ever_sent`, `test_ownership_cannot_change_underneath_a_turn_in_flight` |
| INV-037 | `test_a_tool_that_is_offered_but_not_allowed_is_never_imported`, `test_the_operator_decides_the_risk_and_the_server_cannot_lower_it`, `test_a_changed_input_schema_stops_the_call_before_anything_is_sent`, `test_a_tool_the_server_stopped_offering_is_not_called`, `test_what_cannot_be_represented_exactly_is_refused_by_name`, `test_a_tool_reported_failure_is_classified_conservatively` |
| INV-038 | `test_a_blocked_write_is_a_known_non_execution_never_unknown`, `test_what_the_inner_provider_answers_is_passed_through_unchanged`, `test_business_validation_and_configuration_answers_do_not_count` |
| INV-039 | `test_an_unrecorded_call_is_an_explicit_miss_never_an_invented_answer`, `test_recording_redacts_personal_data_and_drops_operational_metadata` |
| INV-040 | `test_the_pack_never_widens_what_the_model_may_call`, `test_a_pack_never_shadows_or_overrides_what_the_agent_defines`, `test_what_the_pack_requires_of_the_agent_is_checked_before_anything_runs`, `test_a_pack_has_no_place_for_tools_bindings_connections_or_secrets`, `test_the_compiled_agent_is_self_contained_and_records_what_it_installed`, `test_a_host_binding_cannot_lower_what_the_pack_demands`, `test_every_eval_of_the_pack_passes_on_each_host_against_its_real_api` |
| INV-041 | `test_a_stated_time_beyond_the_listed_options_is_still_found`, `test_a_time_said_while_choosing_can_pick_an_option_that_was_not_listed`, `test_a_number_never_picks_an_option_that_was_not_listed` |
| INV-042 | `test_a_server_that_changed_its_tool_after_the_proposal_is_not_called`, `test_a_flow_can_hand_the_conversation_to_a_person` |
| INV-043 | `test_the_trail_is_append_only_in_the_database_itself`, `test_a_detail_that_names_content_is_refused`, `test_an_ownership_change_is_audited_without_naming_the_conversation`, `test_promoting_records_state_history_and_audit_together`, `test_the_erasure_is_audited_without_the_person_or_the_content` |
| INV-044 | `test_a_conversation_with_work_in_progress_is_not_migrated_by_a_rollback`, `test_a_version_must_be_published_and_evidenced_before_it_is_released`, `test_rolling_back_a_canary_withdraws_it_for_good`, `test_an_idle_conversation_follows_a_promotion_and_a_rollback`, `test_the_canary_is_widened_paused_and_graduated` |
| INV-045 | `test_erasing_a_contact_removes_everything_that_names_them`, `test_what_other_guarantees_need_stays_as_a_content_free_tombstone`, `test_a_pending_confirmation_and_an_undelivered_prompt_block_until_forced`, `test_an_unfinished_external_effect_is_never_forced_away`, `test_a_conversation_being_processed_cannot_be_erased` |
| INV-046 | `test_an_overloaded_edge_refuses_without_persisting_and_the_redelivery_is_accepted`, `test_only_new_user_messages_are_shed_state_updates_always_get_through`, `test_a_tenant_burst_is_capped_before_anything_is_sent_even_for_a_write`, `test_the_shared_limiter_holds_across_concurrent_workers` |
| INV-047 | `test_an_applied_migration_that_was_edited_stops_everything`, `test_a_database_ahead_of_this_build_is_refused`, `test_two_migrators_booting_together_apply_each_migration_once`, `test_a_failing_migration_leaves_no_trace_and_nothing_after_it_runs` |
| INV-048 | `test_a_failing_ledger_never_fails_the_call_and_is_counted_and_logged`, `test_a_failing_metrics_backend_never_changes_a_call`, `test_a_failing_metrics_backend_never_changes_what_a_turn_does`, `test_without_a_tracer_spans_cost_nothing_and_a_failing_tracer_changes_nothing` |
| INV-049 | `test_personal_data_is_scrubbed_and_text_is_capped`, `test_the_ledger_holds_no_prompt_or_answer_column`, `test_a_record_cannot_carry_negative_numbers_or_unknown_fields`, `test_attributes_are_scalars_scrubbed_and_capped`, `test_a_storage_failure_at_the_edge_is_logged_with_its_event_and_tenant`, `test_a_conversation_is_named_by_a_stable_reference_never_by_its_id` |
| INV-050 | `test_a_replayed_turn_never_counts_a_call_twice_and_the_books_name_the_agent`, `test_a_call_outside_any_context_is_still_on_the_books_as_unattributed`, `test_a_model_without_a_price_is_counted_as_unpriced_not_free`, `test_cost_is_tokens_times_the_price_in_force_on_the_day_of_the_call` |
| INV-051 | `test_only_a_tenant_over_a_refuse_new_budget_is_refused`, `test_the_edge_refuses_a_budget_exceeded_tenant_before_persisting_anything`, `test_only_a_budget_that_asks_for_it_refuses` |
| INV-052 | `test_asking_for_a_person_hands_the_conversation_over_without_a_model`, `test_asking_for_a_person_hands_over_and_the_bot_stays_quiet`, `test_a_negation_or_a_long_message_is_not_a_request_for_a_person`, `test_an_agent_with_nobody_behind_it_answers_honestly_and_keeps_the_conversation`, `test_an_agent_without_the_block_treats_the_message_like_any_other` |
| INV-053 | `test_a_question_while_the_subject_is_awaited_goes_to_the_model_not_into_the_subject`, `test_a_question_that_really_is_the_subject_is_kept_whole`, `test_a_plain_subject_never_costs_a_model_call`, `test_without_the_opt_in_a_free_text_slot_still_takes_whatever_comes_next`, `test_agents_that_do_not_use_the_new_features_keep_the_digest_they_always_had` |

Os casos `C01`–`C16` (ponto de falha, resultado esperado e fase em que passam a ser exigidos) estão definidos em [`ROADMAP.md`](./ROADMAP.md#chaos-gates).

## Regra de alteração

Uma PR que altera persistência, fencing, idempotência, confirmação, retry ou delivery protocol deve:

1. apontar as invariantes afetadas;
2. atualizar o teste/chaos case correspondente;
3. atualizar `RUNTIME_PROTOCOL.md`;
4. atualizar `ROADMAP.md` se o DoD de alguma fase mudar.
