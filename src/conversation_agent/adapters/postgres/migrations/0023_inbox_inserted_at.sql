-- When the DATABASE saw each inbound event. The debounce window ("wait until the contact stops
-- typing") compares it with the database clock, so a worker whose own clock is off cannot shorten
-- or stretch it. Existing rows get the migration's time.
ALTER TABLE inbox_events ADD COLUMN inserted_at timestamptz NOT NULL DEFAULT clock_timestamp();
