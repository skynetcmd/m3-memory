-- 018_variant.up.sql
-- Adds a first-class `variant` column to memory_items so callers can tag
-- ingested items with a free-form variant identifier (e.g. "baseline",
-- "heuristic_c1c4", "llm_v1"). Enables apples-to-apples comparisons between
-- multiple ingestion strategies that coexist in the same DB.
--
-- Default NULL so legacy rows remain untagged until an explicit backfill.
-- Retrieval callers that need a variant filter should pass it through
-- memory_search_impl; callers that don't care can ignore the column.

ALTER TABLE memory_items ADD COLUMN variant TEXT DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_memory_items_variant
    ON memory_items(variant) WHERE variant IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_memory_items_user_variant
    ON memory_items(user_id, variant) WHERE variant IS NOT NULL;
