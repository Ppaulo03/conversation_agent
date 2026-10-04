-- Phase 6: inbound events carry media REFERENCES (claim-check) and a kind (user | system).
ALTER TABLE inbox_events ADD COLUMN media jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE inbox_events ADD COLUMN kind text NOT NULL DEFAULT 'user'
    CHECK (kind IN ('user', 'system'));