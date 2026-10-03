-- conversation_agent schema v1 (DESIGN §39C). Conversational/operational state only (INV-008).

CREATE TABLE conversation_states (
    tenant_id          text        NOT NULL,
    conversation_id    text        NOT NULL,
    channel_id         text        NOT NULL,
    contact_id         text        NOT NULL,
    session_id         text        NOT NULL,
    version            bigint      NOT NULL DEFAULT 0,
    conversation_epoch bigint      NOT NULL DEFAULT 0,
    lease_owner        text,
    lease_expires_at   timestamptz,
    ownership          text        NOT NULL DEFAULT 'BOT'
                       CHECK (ownership IN ('BOT', 'HANDOFF_PENDING', 'HUMAN')),
    state_json         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    last_event_at      timestamptz,
    agent_version      text,
    updated_at         timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, conversation_id)
);
CREATE INDEX conversation_states_contact ON conversation_states (tenant_id, contact_id);

CREATE TABLE turns (
    tenant_id        text        NOT NULL,
    conversation_id  text        NOT NULL,
    turn_id          text        NOT NULL,
    status           text        NOT NULL
                     CHECK (status IN ('PROCESSING', 'COMPLETED', 'FAILED', 'CANCELLED')),
    event_ids        jsonb       NOT NULL,
    late_event_ids   jsonb       NOT NULL DEFAULT '[]'::jsonb,
    user_text        text        NOT NULL,
    attempts         integer     NOT NULL DEFAULT 1,
    created_epoch    bigint      NOT NULL,
    created_at       timestamptz NOT NULL,
    completed_at     timestamptz,
    failure_reason   text,
    PRIMARY KEY (tenant_id, turn_id)
);
-- at most one open turn per conversation
CREATE UNIQUE INDEX turns_one_open_per_conversation
    ON turns (tenant_id, conversation_id) WHERE status = 'PROCESSING';

CREATE TABLE turn_journal (
    tenant_id        text        NOT NULL,
    conversation_id  text        NOT NULL,
    turn_id          text        NOT NULL,
    step_index       integer     NOT NULL,
    step_type        text        NOT NULL,
    request_hash     text,
    logical_step_id  text,
    payload          jsonb       NOT NULL,
    created_at       timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, conversation_id, turn_id, step_index)
);

CREATE TABLE inbox_events (
    id               bigserial   PRIMARY KEY,
    tenant_id        text        NOT NULL,
    channel_id       text        NOT NULL,
    event_id         text        NOT NULL,
    conversation_id  text        NOT NULL,
    contact_id       text        NOT NULL,
    session_id       text        NOT NULL,
    source_sequence  bigint,
    occurred_at      timestamptz NOT NULL,
    received_at      timestamptz NOT NULL,
    text             text        NOT NULL,
    status           text        NOT NULL DEFAULT 'READY'
                     CHECK (status IN ('RECEIVED', 'READY', 'CLAIMED', 'CONSUMED', 'RETRY', 'DEAD')),
    claimed_by       text,
    claim_epoch      bigint,
    turn_id          text,
    UNIQUE (tenant_id, channel_id, event_id)
);
CREATE INDEX inbox_events_pending
    ON inbox_events (tenant_id, conversation_id, status)
    WHERE status IN ('READY', 'CLAIMED');

CREATE TABLE outbox_messages (
    tenant_id             text        NOT NULL,
    outbox_id             text        NOT NULL,
    conversation_id       text        NOT NULL,
    channel_id            text        NOT NULL,
    contact_id            text        NOT NULL,
    turn_id               text        NOT NULL,
    message_index         integer     NOT NULL,
    action_id             text,
    text                  text        NOT NULL,
    idempotency_key       text        NOT NULL,
    status                text        NOT NULL DEFAULT 'PENDING'
                          CHECK (status IN ('PENDING', 'SENDING', 'QUEUED', 'ACCEPTED', 'FAILED',
                                            'UNKNOWN', 'RECONCILING', 'SUPERSEDED')),
    attempts              integer     NOT NULL DEFAULT 0,
    available_at          timestamptz NOT NULL,
    claim_owner           text,
    claim_expires_at      timestamptz,
    provider_message_id   text,
    provider_accepted_at  timestamptz,
    last_error            text,
    created_at            timestamptz NOT NULL,
    updated_at            timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, outbox_id),
    UNIQUE (tenant_id, conversation_id, turn_id, message_index)
);
CREATE INDEX outbox_messages_ready ON outbox_messages (status, available_at);

CREATE TABLE tool_invocations (
    tenant_id                  text        NOT NULL,
    invocation_id              text        NOT NULL,
    conversation_id            text        NOT NULL,
    session_id                 text        NOT NULL,
    turn_id                    text        NOT NULL,
    logical_step_id            text        NOT NULL,
    attempt_semantic_id        text        NOT NULL,
    action_id                  text,
    tool_name                  text        NOT NULL,
    capability                 text        NOT NULL,
    args_hash                  text        NOT NULL,
    idempotency_key            text        NOT NULL,
    request_json               jsonb       NOT NULL,
    context_json               jsonb       NOT NULL,
    status                     text        NOT NULL
                               CHECK (status IN ('PREPARED', 'EXECUTING', 'SUCCEEDED', 'FAILED',
                                                 'UNKNOWN', 'RECONCILING', 'RECONCILED',
                                                 'HUMAN_HANDOFF')),
    execution_owner            text,
    execution_lease_expires_at timestamptz,
    execution_epoch            bigint      NOT NULL DEFAULT 0,
    result_application_status  text        NOT NULL DEFAULT 'none'
                               CHECK (result_application_status IN ('none', 'pending', 'applied')),
    result_json                jsonb,
    error_json                 jsonb,
    provider_metadata          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    prepared_at                timestamptz NOT NULL,
    started_at                 timestamptz,
    finished_at                timestamptz,
    applied_at                 timestamptz,
    PRIMARY KEY (tenant_id, invocation_id)
);
CREATE UNIQUE INDEX tool_invocations_action
    ON tool_invocations (tenant_id, action_id) WHERE action_id IS NOT NULL;
CREATE INDEX tool_invocations_recovery
    ON tool_invocations (status) WHERE status IN ('EXECUTING', 'UNKNOWN', 'RECONCILING');
CREATE INDEX tool_invocations_pending_application
    ON tool_invocations (tenant_id, conversation_id) WHERE result_application_status = 'pending';

CREATE TABLE scheduled_events (
    tenant_id        text        NOT NULL,
    scheduler_key    text        NOT NULL,
    event_type       text        NOT NULL,
    payload          jsonb       NOT NULL DEFAULT '{}'::jsonb,
    due_at           timestamptz NOT NULL,
    status           text        NOT NULL DEFAULT 'PENDING'
                     CHECK (status IN ('PENDING', 'CLAIMED', 'DONE', 'CANCELLED')),
    claimed_by       text,
    claim_expires_at timestamptz,
    created_at       timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, scheduler_key)
);
CREATE INDEX scheduled_events_due ON scheduled_events (due_at) WHERE status IN ('PENDING', 'CLAIMED');
