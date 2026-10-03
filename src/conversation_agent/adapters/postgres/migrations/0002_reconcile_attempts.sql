-- How many times reconciliation has claimed an invocation (bounds automatic recovery).
ALTER TABLE tool_invocations ADD COLUMN reconcile_attempts integer NOT NULL DEFAULT 0;