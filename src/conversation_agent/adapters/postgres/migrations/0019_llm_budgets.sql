-- Phase 11: per-tenant LLM budgets (tokens and/or USD, per day and per month). Changing one is an
-- administrative act and is audited in the same transaction.
CREATE TABLE llm_budgets (
    tenant_id  text        PRIMARY KEY,
    budget     jsonb       NOT NULL,
    updated_by text        NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
