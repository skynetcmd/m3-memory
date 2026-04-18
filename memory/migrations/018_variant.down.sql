-- 018_variant.down.sql
-- Reverts 018_variant.up.sql.

DROP INDEX IF EXISTS idx_memory_items_user_variant;
DROP INDEX IF EXISTS idx_memory_items_variant;

ALTER TABLE memory_items DROP COLUMN variant;
