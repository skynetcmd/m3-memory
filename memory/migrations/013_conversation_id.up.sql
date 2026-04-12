-- 013_conversation_id.up.sql
-- Adds conversation_id to memory_items so memories can be grouped by
-- conversation / team session. Uses the same ID space as the existing
-- conversation_start / conversation_append tools — one concept, not two.
--
-- Nullable and indexed. Existing rows are intentionally left NULL; backfill
-- would be guesswork and the whole point of this column is going forward.

ALTER TABLE memory_items ADD COLUMN conversation_id TEXT;

CREATE INDEX IF NOT EXISTS idx_mi_conversation_id
    ON memory_items(conversation_id);
