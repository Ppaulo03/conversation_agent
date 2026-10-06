-- A `defer_llm` budget pauses an already accepted turn until the period resets. It remains the
-- conversation's one open turn, so later messages stay ordered behind it.
ALTER TABLE turns ADD COLUMN deferred_until timestamptz;
CREATE INDEX turns_deferred_until ON turns (deferred_until) WHERE status = 'PROCESSING';
