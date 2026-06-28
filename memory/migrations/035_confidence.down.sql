-- 035_confidence.down.sql
-- Reverts 035_confidence.up.sql.

DROP INDEX IF EXISTS idx_memory_items_confidence;

ALTER TABLE memory_items DROP COLUMN contradiction_count;
ALTER TABLE memory_items DROP COLUMN corroboration_count;
ALTER TABLE memory_items DROP COLUMN belief_beta;
ALTER TABLE memory_items DROP COLUMN belief_alpha;
ALTER TABLE memory_items DROP COLUMN confidence;
