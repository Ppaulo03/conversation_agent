-- Phase 11: the books of LLM spend. One row per call, append-only (enforced by the database):
-- identifiers and numbers only, never prompts or answers, so it is not personal data and is kept
-- for as long as the accounts must be kept. The conversation appears only as a non-reversible ref.
CREATE TABLE llm_usage (
    id                 bigserial        PRIMARY KEY,
    tenant_id          text             NOT NULL,
    started_at         timestamptz      NOT NULL,
    purpose            text             NOT NULL,
    agent_id           text,
    agent_version      text,
    conversation_ref   text,
    turn_id            text,
    trace_id           text,
    request_id         text,
    provider           text,
    model              text,
    input_tokens       integer          NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens      integer          NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cache_read_tokens  integer          NOT NULL DEFAULT 0 CHECK (cache_read_tokens >= 0),
    cache_write_tokens integer          NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    reasoning_tokens   integer          NOT NULL DEFAULT 0 CHECK (reasoning_tokens >= 0),
    latency_ms         double precision NOT NULL DEFAULT 0,
    outcome            text             NOT NULL CHECK (outcome IN ('ok', 'error')),
    error_code         text,
    stop_reason        text
);
CREATE INDEX llm_usage_by_tenant_time ON llm_usage (tenant_id, started_at);
CREATE INDEX llm_usage_by_agent_version ON llm_usage (tenant_id, agent_id, agent_version, started_at);

CREATE FUNCTION llm_usage_is_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'the LLM usage ledger is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER llm_usage_no_update_delete
    BEFORE UPDATE OR DELETE ON llm_usage
    FOR EACH ROW EXECUTE FUNCTION llm_usage_is_append_only();
