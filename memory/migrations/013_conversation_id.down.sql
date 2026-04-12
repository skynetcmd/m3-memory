-- 013_conversation_id.down.sql
-- Reverses 013 by dropping the conversation_id column + its index.
-- Requires SQLite >= 3.35 for ALTER TABLE DROP COLUMN.

DROP INDEX IF EXISTS idx_mi_conversation_id;

ALTER TABLE memory_items DROP COLUMN conversation_id;
