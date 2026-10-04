-- Phase 10: retention and erasure (LGPD, DESIGN 38).
--   tenant_retention  per-tenant retention windows (what `apply_retention` enforces)
--   contact indexes   locate everything a contact left behind by (tenant_id, contact_id)
CREATE TABLE tenant_retention (
    tenant_id  text        PRIMARY KEY,
    policy     jsonb       NOT NULL,
    updated_by text        NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX inbox_events_by_contact ON inbox_events (tenant_id, contact_id);
CREATE INDEX outbox_messages_by_contact ON outbox_messages (tenant_id, contact_id);
CREATE INDEX scheduled_events_by_contact ON scheduled_events (tenant_id, (payload->>'contact_id'));
