-- Phase 10: administrative audit trail. Append-only, enforced by the database itself: what an
-- operator did (who, what, to what, with what outcome) never changes after the fact. It holds
-- identifiers and counts, never message content (LGPD: a minimal trail, DESIGN 38).
CREATE TABLE admin_audit (
    id           bigserial   PRIMARY KEY,
    tenant_id    text        NOT NULL,
    occurred_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    actor        text        NOT NULL,
    action       text        NOT NULL,
    subject_type text        NOT NULL,
    subject_id   text        NOT NULL,
    outcome      text        NOT NULL DEFAULT 'ok',
    details      jsonb       NOT NULL DEFAULT '{}'
);
CREATE INDEX admin_audit_by_tenant_time ON admin_audit (tenant_id, occurred_at DESC);
CREATE INDEX admin_audit_by_subject ON admin_audit (tenant_id, subject_type, subject_id);

CREATE FUNCTION admin_audit_is_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'the admin audit trail is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER admin_audit_no_update_delete
    BEFORE UPDATE OR DELETE ON admin_audit
    FOR EACH ROW EXECUTE FUNCTION admin_audit_is_append_only();
