-- 029_enrichment_content_size.up.sql
--
-- Adds enrichment_groups.content_size_k for size-aware re-runs.
--
-- The integer is total source content rounded UP to KB. Lets you process
-- the small majority at high concurrency, then come back for the bigger
-- groups with lower concurrency / a higher per-slot context budget so
-- they don't fail on context-length limits.
--
-- Idempotent under bin/migrate_memory.py: re-apply triggers the
-- "duplicate column name" warning path, which the runner treats as
-- already-applied.

ALTER TABLE enrichment_groups ADD COLUMN content_size_k INTEGER;

CREATE INDEX IF NOT EXISTS idx_eg_size
    ON enrichment_groups(source_variant, target_variant, content_size_k);
