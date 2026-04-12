-- 015_conversation_id_composite_index.down.sql
-- Reverts to the plain index from migration 013.

DROP INDEX IF EXISTS idx_mi_conversation_id;

CREATE INDEX IF NOT EXISTS idx_mi_conversation_id
    ON memory_items(conversation_id);
