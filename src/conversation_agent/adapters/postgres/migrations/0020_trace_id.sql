-- Phase 11: one correlation id from the moment an inbound event enters the system until the reply
-- it caused is sent. Minted at the edge, carried by the inbox row, the turn, the tool calls and the
-- outbox row. Nullable: rows from before this migration simply have none.
ALTER TABLE inbox_events ADD COLUMN trace_id text;
ALTER TABLE outbox_messages ADD COLUMN trace_id text;
