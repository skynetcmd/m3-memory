-- 014_refresh_on.up.sql
-- Adds refresh_on (ISO 8601 timestamp) and refresh_reason to memory_items.
--
-- When refresh_on <= now, memory_maintenance surfaces the memory for review
-- via the existing memory_inbox. The actual refresh flows through memory_update,
-- which already versions via memory_history — no parallel soft-delete/reinsert
-- lifecycle. refresh_on is a signal, not a separate mechanism.

ALTER TABLE memory_items ADD COLUMN refresh_on TEXT;
ALTER TABLE memory_items ADD COLUMN refresh_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_mi_refresh_on
    ON memory_items(refresh_on)
    WHERE refresh_on IS NOT NULL;
