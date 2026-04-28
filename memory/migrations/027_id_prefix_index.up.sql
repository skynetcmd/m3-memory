-- 027_id_prefix_index.up.sql
--
-- Conversations and resume-guides routinely reference memories by their
-- 8-char id prefix (e.g. `0906f86c`). Today there is no index supporting
-- prefix lookups, so `WHERE SUBSTR(id,1,8) = ?` falls back to a full scan
-- of memory_items. memory_get_impl now accepts an 8-char prefix as a
-- first-class input — this expression index makes that lookup O(log n).
--
-- Idempotent: IF NOT EXISTS lets `migrate up` be re-run safely after a
-- partial failure without tripping the partial-apply detector.

CREATE INDEX IF NOT EXISTS idx_mi_id_prefix8
    ON memory_items(SUBSTR(id, 1, 8));
