-- Phase 6: the delivery contract needs to know when a row was first sent (idempotency window)
-- and how often an UNKNOWN row has been reconciled.
ALTER TABLE outbox_messages ADD COLUMN first_sent_at timestamptz;
ALTER TABLE outbox_messages ADD COLUMN reconcile_attempts integer NOT NULL DEFAULT 0;