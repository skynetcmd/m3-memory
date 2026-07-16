-- pg_042_enrichment_group_columns.down.sql — revert pg_042.
DROP INDEX IF EXISTS idx_eg_size;
ALTER TABLE enrichment_groups DROP COLUMN IF EXISTS partial_failure_chunks;
ALTER TABLE enrichment_groups DROP COLUMN IF EXISTS content_size_k;
