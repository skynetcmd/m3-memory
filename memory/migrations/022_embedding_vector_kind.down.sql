-- 022_embedding_vector_kind.down.sql
--
-- Reverses v022. Safe because:
--  - Dropping the index is always reversible.
--  - Dropping a NOT NULL column with a default doesn't orphan data
--    (pre-v022 rows had no concept of vector_kind).
--
-- WARNING: if rows with vector_kind != 'default' exist when this runs,
-- they become indistinguishable from 'default' rows. Callers writing
-- multiple vectors per memory_id under v022+ should NOT downgrade
-- until those rows are either deleted or merged.

DROP INDEX IF EXISTS idx_me_memory_kind;
ALTER TABLE memory_embeddings DROP COLUMN vector_kind;
