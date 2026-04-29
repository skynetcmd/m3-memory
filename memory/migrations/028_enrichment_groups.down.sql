-- 028_enrichment_groups.down.sql
--
-- Reverts 028_enrichment_groups.up.sql.
-- SQLite cannot DROP COLUMN before 3.35; we use the table-rebuild idiom for
-- memory_items.source_group_id. Tables and their indexes drop cleanly.

DROP INDEX IF EXISTS idx_mi_source_group;
DROP INDEX IF EXISTS idx_eg_claim;
DROP INDEX IF EXISTS idx_eg_eligible;
DROP INDEX IF EXISTS idx_eg_run;
DROP INDEX IF EXISTS idx_eg_status_variant;
DROP INDEX IF EXISTS idx_er_status;
DROP INDEX IF EXISTS idx_er_started;

DROP TABLE IF EXISTS enrichment_groups;
DROP TABLE IF EXISTS enrichment_runs;

-- SQLite ≥3.35 supports DROP COLUMN directly. Fall through to the rebuild
-- idiom on older versions if needed; for now we rely on a recent SQLite.
ALTER TABLE memory_items DROP COLUMN source_group_id;
