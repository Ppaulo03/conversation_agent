-- `attempts` now counts genuine processing FAILURES (e.g. LLM provider errors). Waiting for an
-- external result (tool executing / unknown / reconciling) is not a failure and never counts.
ALTER TABLE turns ALTER COLUMN attempts SET DEFAULT 0;
UPDATE turns SET attempts = 0 WHERE status = 'PROCESSING';