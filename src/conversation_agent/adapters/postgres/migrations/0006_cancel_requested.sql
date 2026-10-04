-- Phase 4: cooperative cancellation (DESIGN 20.2, RUNTIME_PROTOCOL 10).
-- A new message under the `restart` policy sets the flag while a turn is processing; the
-- worker reads it with its heartbeat and honours it only at a safe step boundary.
ALTER TABLE conversation_states ADD COLUMN cancel_requested boolean NOT NULL DEFAULT false;
