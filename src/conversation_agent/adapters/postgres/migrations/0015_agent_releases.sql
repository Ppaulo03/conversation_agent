-- Phase 10: release management. `agent_release_state` is what new conversations resolve against;
-- `agent_release_history` is the append-only record of every release decision (who, why, with
-- which eval evidence or which version it came from). Publishing a version does not release it.
CREATE TABLE agent_release_state (
    tenant_id         text        NOT NULL,
    agent_id          text        NOT NULL,
    stable            text        NOT NULL,
    candidate         text,
    candidate_percent integer     NOT NULL DEFAULT 0 CHECK (candidate_percent BETWEEN 0 AND 100),
    withdrawn         text[]      NOT NULL DEFAULT '{}',
    updated_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, agent_id),
    CHECK (candidate IS NOT NULL OR candidate_percent = 0)
);

CREATE TABLE agent_release_history (
    id         bigserial   PRIMARY KEY,
    tenant_id  text        NOT NULL,
    agent_id   text        NOT NULL,
    event      text        NOT NULL,
    version    text        NOT NULL,
    percent    integer,
    actor      text        NOT NULL,
    reason     text        NOT NULL,
    detail     jsonb       NOT NULL DEFAULT '{}',
    at         timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX agent_release_history_by_agent ON agent_release_history (tenant_id, agent_id, id);

CREATE FUNCTION agent_release_history_is_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'the release history is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER agent_release_history_no_update_delete
    BEFORE UPDATE OR DELETE ON agent_release_history
    FOR EACH ROW EXECUTE FUNCTION agent_release_history_is_append_only();
