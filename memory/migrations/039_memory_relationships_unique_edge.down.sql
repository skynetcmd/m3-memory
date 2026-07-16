-- 039_memory_relationships_unique_edge.down.sql
-- Reverses 039 by dropping the unique-edge index. The dedup in the up-migration
-- is NOT reversible (deleted duplicate rows are gone), which is correct: the
-- duplicates were unintended and carried no distinct information (same from_id,
-- to_id, relationship_type; the only differing column was the random `id` PK).
DROP INDEX IF EXISTS idx_mr_unique_edge;
