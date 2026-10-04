-- Phase 10: shared token buckets, so a rate limit holds across every worker process. The refill
-- uses the database clock (INV-032): no worker's wall clock decides how many tokens exist.
CREATE TABLE rate_limit_buckets (
    scope      text             NOT NULL,
    key        text             NOT NULL,
    tokens     double precision NOT NULL,
    refilled_at timestamptz     NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (scope, key)
);
CREATE INDEX rate_limit_buckets_by_age ON rate_limit_buckets (refilled_at);
