-- Phase 5: immutable, versioned agents. The manifest is the source of truth; the digest proves
-- that what is loaded later is exactly the definition that was published.
CREATE TABLE published_agents (
    agent_id         text        NOT NULL,
    version          text        NOT NULL,
    digest           text        NOT NULL,
    manifest_json    jsonb       NOT NULL,
    schema_version   integer     NOT NULL,
    compiler_version text        NOT NULL,
    published_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, version)
);

CREATE FUNCTION published_agents_are_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'published agents are immutable (% %)', OLD.agent_id, OLD.version;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER published_agents_no_update_delete
    BEFORE UPDATE OR DELETE ON published_agents
    FOR EACH ROW EXECUTE FUNCTION published_agents_are_immutable();
