-- 015_conversation_id_composite_index.up.sql
-- Replaces the plain idx_mi_conversation_id with a composite partial index
-- on (conversation_id, created_at) WHERE is_deleted = 0.
--
-- Why: on a populated table, the planner was choosing idx_mi_deleted (a very
-- low-cardinality index) for WHERE conversation_id = ? AND is_deleted = 0
-- because that was the "best" option given the available indexes. The partial
-- composite:
--   * is much smaller (only live rows)
--   * has higher selectivity on conversation_id (leading column)
--   * satisfies ORDER BY created_at for free (no sort step)
--
-- Verified on a 1000-row synthetic table: planner now picks the new index.

DROP INDEX IF EXISTS idx_mi_conversation_id;

CREATE INDEX IF NOT EXISTS idx_mi_conversation_id
    ON memory_items(conversation_id, created_at)
    WHERE is_deleted = 0;
