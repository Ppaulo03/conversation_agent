-- Phase 5.2: the agent registry is TENANT-SCOPED: two tenants may both publish an agent called
-- support with different definitions. 0007 never shipped with data, so the table is recreated.
DROP TABLE published_agents;
DROP FUNCTION published_agents_are_immutable();

CREATE TABLE published_agents (
    tenant_id        text        NOT NULL,
    agent_id         text        NOT NULL,
    version          text        NOT NULL,
    digest           text        NOT NULL,
    manifest_json    jsonb       NOT NULL,
    schema_version   integer     NOT NULL,
    compiler_version text        NOT NULL,
    published_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, agent_id, version)
);

CREATE FUNCTION published_agents_are_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'published agents are immutable (% % %)', OLD.tenant_id, OLD.agent_id, OLD.version;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER published_agents_no_update_delete
    BEFORE UPDATE OR DELETE ON published_agents
    FOR EACH ROW EXECUTE FUNCTION published_agents_are_immutable();
