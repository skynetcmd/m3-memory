-- 026_enrichment_stage.up.sql
--
-- Forward-compat: add a `stage` column to observation_queue and
-- reflector_queue so future multi-stage enrichment (entity_consolidator,
-- timeline_validator, etc.) can fold into the same queue tables without
-- another migration. Default values match the current single-stage
-- meaning so existing drainer code continues to behave identically.
--
-- Why now (and not later):
-- (1) Cheap — two ALTERs and two backfill UPDATEs.
-- (2) Lets us add stage 3 without renaming tables or copying rows.
-- (3) The next-stage drainer can filter `WHERE stage='entity_consolidator'`
--     against the same indexes that already support attempts ordering.
--
-- This does NOT unify the two queue tables into one yet — Option B in
-- the multi-stage architecture review (memory 9f5033b8) is deferred
-- until we have ≥3 real stages. This migration just removes the
-- biggest barrier to that future merge.
--
-- Idempotence note: SQLite has no `ADD COLUMN IF NOT EXISTS`. We rely
-- on migrate_memory.py's schema_migrations table to skip re-applying
-- v026 on re-runs. If you need to re-run after a partial failure,
-- roll back via the .down.sql first.

ALTER TABLE observation_queue ADD COLUMN stage TEXT NOT NULL DEFAULT 'observer';
ALTER TABLE reflector_queue   ADD COLUMN stage TEXT NOT NULL DEFAULT 'reflector';

-- Backfill (DEFAULT handles new rows; this covers any rows present at
-- migration time since SQLite's DEFAULT only applies to rows inserted
-- AFTER the ALTER).
UPDATE observation_queue SET stage='observer'  WHERE stage IS NULL OR stage='';
UPDATE reflector_queue   SET stage='reflector' WHERE stage IS NULL OR stage='';

-- Index that future stages will benefit from. Filtering by stage is
-- already cheap (low cardinality), but indexing avoids a full scan
-- when both queues hold many stages.
CREATE INDEX IF NOT EXISTS idx_oq_stage_attempts
    ON observation_queue(stage, attempts, enqueued_at);
CREATE INDEX IF NOT EXISTS idx_rq_stage_attempts
    ON reflector_queue(stage, attempts, enqueued_at);
