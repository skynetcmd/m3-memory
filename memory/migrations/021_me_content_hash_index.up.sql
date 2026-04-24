-- 021_me_content_hash_index.up.sql
--
-- Add composite index on memory_embeddings(content_hash, embed_model).
--
-- The embed cache lookup in _embed_many / _embed uses:
--   WHERE embed_model = ? AND content_hash IN (?, ?, ...)
-- Without this index, that query does a full table scan on large
-- memory_embeddings tables — a multi-second stall per batch of cache
-- hits. With the composite index the planner does an index seek and
-- the same lookup completes in sub-millisecond time.
--
-- content_hash leads the composite because the IN-list is the selective
-- predicate; embed_model follows for the equality filter. An index on
-- (embed_model, content_hash) would also work but embed_model usually
-- has very few distinct values (1–3 in practice), making it a poor lead.

CREATE INDEX IF NOT EXISTS idx_me_content_hash_model
    ON memory_embeddings(content_hash, embed_model);

ANALYZE memory_embeddings;
