-- pg_042_enrichment_group_columns.up.sql
--
-- Adds the enrichment_groups columns from SQLite migrations 029 (content_size_k)
-- and 030 (partial_failure_chunks) that the ported enrichment_state.py references
-- (compute_content_size_k enrollment, mark_success partial-failure accounting).
-- The v39 baseline + pg_040 translated migration 028's shape but not these two
-- follow-on ALTERs.
--
-- ADD COLUMN IF NOT EXISTS is idempotent; one implicit transaction (migrate_pg
-- wraps the file), no explicit BEGIN/COMMIT.

ALTER TABLE enrichment_groups ADD COLUMN IF NOT EXISTS content_size_k INTEGER;
ALTER TABLE enrichment_groups ADD COLUMN IF NOT EXISTS partial_failure_chunks INTEGER DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_eg_size
    ON enrichment_groups(source_variant, target_variant, content_size_k);
