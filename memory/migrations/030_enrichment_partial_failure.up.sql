-- 030_enrichment_partial_failure.up.sql
--
-- Adds enrichment_groups.partial_failure_chunks for visibility into multi-chunk
-- groups whose source was split into N chunks but K<N succeeded. The row is
-- still marked status='success' (the partial observations are valid and worth
-- keeping) but this counter flags it for later audit / re-extraction.
--
-- Default 0 means "no chunks failed" — the common case. Non-zero means K
-- chunks of the group's M total were dropped on this enrichment pass.
--
-- Idempotent under bin/migrate_memory.py: re-apply triggers "duplicate column
-- name" which the runner treats as already-applied.

ALTER TABLE enrichment_groups ADD COLUMN partial_failure_chunks INTEGER DEFAULT 0;
