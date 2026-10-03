-- Phase 3: PendingAction / ActionConfirmation and the channel evidence needed to prove that a
-- reply belongs to an eligible (ACCEPTED) confirmation prompt.

CREATE TABLE pending_actions (
    tenant_id               text        NOT NULL,
    conversation_id         text        NOT NULL,
    action_id               text        NOT NULL,
    capability              text        NOT NULL,
    tool_name               text        NOT NULL,
    request_json            jsonb       NOT NULL,
    args_hash               text        NOT NULL,
    protected_fields        jsonb       NOT NULL,
    summary                 text        NOT NULL,
    created_from_turn       text        NOT NULL,
    latest_prompt_outbox_id text,
    confirmation_attempts   integer     NOT NULL DEFAULT 0,
    expires_at              timestamptz,
    status                  text        NOT NULL
                            CHECK (status IN ('PENDING_CONFIRMATION', 'CONFIRMED', 'REJECTED',
                                              'INVALIDATED', 'EXPIRED')),
    created_at              timestamptz NOT NULL,
    updated_at              timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, action_id)  -- UNIQUE(tenant_id, action_id): protected action identity
);
-- one action awaiting confirmation per conversation at a time
CREATE UNIQUE INDEX pending_actions_one_awaiting
    ON pending_actions (tenant_id, conversation_id) WHERE status = 'PENDING_CONFIRMATION';

CREATE TABLE action_confirmations (
    tenant_id                    text        NOT NULL,
    action_id                    text        NOT NULL,
    inbound_message_id           text        NOT NULL,
    prompt_outbox_id             text        NOT NULL,
    reply_to_provider_message_id text,
    decision                     text        NOT NULL
                                 CHECK (decision IN ('confirm', 'reject', 'modify', 'unclear')),
    interpreter                  text        NOT NULL CHECK (interpreter IN ('rule', 'llm')),
    confidence                   double precision,
    occurred_at                  timestamptz NOT NULL,
    confirmed_at                 timestamptz,
    created_at                   timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, action_id, inbound_message_id)
);

ALTER TABLE inbox_events ADD COLUMN reply_to_provider_message_id text;
ALTER TABLE inbox_events ADD COLUMN provider_occurred_at timestamptz;