-- 014_refresh_on.down.sql
-- Reverses 014 by dropping refresh_on, refresh_reason, and the index.
-- Requires SQLite >= 3.35 for ALTER TABLE DROP COLUMN.

DROP INDEX IF EXISTS idx_mi_refresh_on;

ALTER TABLE memory_items DROP COLUMN refresh_on;
ALTER TABLE memory_items DROP COLUMN refresh_reason;
