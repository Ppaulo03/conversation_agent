-- What the agent remembers about a contact across conversations (name and short facts). Keyed by
-- (tenant, contact) and independent of any conversation: a conversation reset keeps it, erasing
-- the contact removes it. `fact_seq` only grows, so a fact id is never reused after a forget.
CREATE TABLE contact_profiles (
    tenant_id    text        NOT NULL,
    contact_id   text        NOT NULL,
    display_name text,
    facts        jsonb       NOT NULL DEFAULT '[]'::jsonb,  -- [{"id": "f1", "text": "..."}]
    fact_seq     integer     NOT NULL DEFAULT 0,
    updated_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant_id, contact_id)
);
CREATE INDEX contact_profiles_by_age ON contact_profiles (tenant_id, updated_at);
