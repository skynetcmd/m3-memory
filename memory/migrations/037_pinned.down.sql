-- 037_pinned.down.sql
-- Reverts 037_pinned.up.sql.

DROP INDEX IF EXISTS idx_memory_items_pinned;

ALTER TABLE memory_items DROP COLUMN pinned;
