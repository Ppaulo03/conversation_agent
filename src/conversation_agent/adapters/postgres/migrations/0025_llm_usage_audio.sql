-- Phase 15: speech-to-text is billed by the SECOND of audio, not by tokens. The books keep the
-- seconds (a number, never the audio) and the price table says what a minute costs.
-- (The table is append-only: adding a column with a default is allowed, rewriting rows is not.)
ALTER TABLE llm_usage ADD COLUMN audio_seconds double precision NOT NULL DEFAULT 0
    CHECK (audio_seconds >= 0);
