-- INV-023: the concrete external operation is frozen when the invocation is PREPARED.
ALTER TABLE tool_invocations ADD COLUMN intent_json jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE tool_invocations ALTER COLUMN intent_json DROP DEFAULT;