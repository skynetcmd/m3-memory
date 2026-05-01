-- 030_enrichment_partial_failure.down.sql

ALTER TABLE enrichment_groups DROP COLUMN partial_failure_chunks;
