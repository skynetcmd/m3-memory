-- pg_044_parity_gdpr_and_stage.up.sql
--
-- Brings the PostgreSQL primary schema back into parity with SQLite for two
-- tables the hand-maintained PG schema had drifted on (caught by
-- tests/test_schema_parity_pg_live.py):
--
--   1. gdpr_requests — created by SQLite migration 010_tier_features.sql but
--      NEVER mirrored into the PG baseline. gdpr_export_impl / gdpr_forget_impl
--      log each request here; without the table those INSERTs silently no-op on
--      PG (the impls swallow the error), so the compliance audit trail was lost.
--   2. observation_queue.stage / reflector_queue.stage — added by SQLite
--      migration 026_enrichment_stage.up.sql for the multi-stage enrichment
--      drainer, missing on PG.
--
-- Type mapping mirrors the SQLite intent: TEXT->TEXT, INTEGER->BIGINT, the
-- SQLite strftime() default -> NOW(). ADD COLUMN / CREATE TABLE IF NOT EXISTS
-- keep this idempotent; migrate_pg wraps the file in one transaction.

CREATE TABLE IF NOT EXISTS gdpr_requests (
    id              TEXT PRIMARY KEY,
    subject_id      TEXT NOT NULL,
    request_type    TEXT NOT NULL,
    status          TEXT DEFAULT 'pending',
    items_affected  BIGINT DEFAULT 0,
    requested_at    TIMESTAMPTZ DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_gdpr_subject ON gdpr_requests(subject_id);

ALTER TABLE observation_queue ADD COLUMN IF NOT EXISTS stage TEXT NOT NULL DEFAULT 'observer';
ALTER TABLE reflector_queue   ADD COLUMN IF NOT EXISTS stage TEXT NOT NULL DEFAULT 'reflector';

CREATE INDEX IF NOT EXISTS idx_oq_stage_attempts
    ON observation_queue(stage, attempts, enqueued_at);
CREATE INDEX IF NOT EXISTS idx_rq_stage_attempts
    ON reflector_queue(stage, attempts, enqueued_at);
