-- 011_handoff_primitive.sql
-- Adds read/unread tracking for handoff-type memories (inbox semantics).
-- The read_at column is semantically meaningful only for rows where type='handoff'.
-- Additive-only migration; safe to re-run (migrate_memory.py treats duplicate columns as idempotent).

ALTER TABLE memory_items ADD COLUMN read_at TEXT DEFAULT NULL;

-- Fast inbox lookup: filter by agent_id + type='handoff' + unread, ordered by created_at.
CREATE INDEX IF NOT EXISTS idx_mi_handoff_inbox
    ON memory_items(agent_id, type, read_at, created_at);
