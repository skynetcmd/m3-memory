-- 037_pinned.up.sql
-- Adds the `pinned` flag to memory_items: a pinned memory is exempt from decay,
-- expiry, and retention purges ("this is canon; don't let lifecycle logic age or
-- delete it"). See the memory-legibility program (Lever 3a).
--
-- Follows 036. Pure additive, non-rewriting: existing rows default pinned=0
-- (unpinned = today's behavior), so flag-off behavior is byte-identical. Same
-- add-column-with-neutral-default precedent as 035.
--
-- The maintenance queries honor this as `COALESCE(pinned,0)=0`; a runtime
-- ADD COLUMN fallback (memory/db.py::ensure_pinned_column) also creates it so a
-- DB touched before migrating degrades gracefully — this migration is the
-- authoritative source of the column for the pre-floor -> 2026.7.1 upgrade path.
--
--   pinned  INTEGER DEFAULT 0  — 1 = exempt from decay/expiry/retention. 0 = normal.

ALTER TABLE memory_items ADD COLUMN pinned INTEGER DEFAULT 0;

-- Partial index: only pinned rows pay index cost. Supports the "skip pinned"
-- filter in decay/expiry/retention without scanning the unpinned majority.
CREATE INDEX IF NOT EXISTS idx_memory_items_pinned
    ON memory_items(pinned) WHERE pinned = 1;
