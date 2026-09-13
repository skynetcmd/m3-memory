-- 006_embedding_vector_kind.down.sql
-- Reverses 006 by dropping vector_kind and its index from the chatlog lane.
-- Requires SQLite >= 3.35 for ALTER TABLE DROP COLUMN (same floor as the
-- main lane's 022 rollback).
--
-- Rolling this back re-opens the defect it fixed: the shared embedding
-- writer will raise "no column named vector_kind" on every chatlog write
-- again. Only roll back in tandem with the main lane.

DROP INDEX IF EXISTS idx_me_memory_kind;

ALTER TABLE memory_embeddings DROP COLUMN vector_kind;
