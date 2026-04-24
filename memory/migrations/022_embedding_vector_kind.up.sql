-- 022_embedding_vector_kind.up.sql
--
-- Add `vector_kind` column to memory_embeddings so a single memory_item
-- can have multiple embedding vectors — one per kind. Intended to
-- support dual-embedding ingest patterns that write one vector from
-- raw `content` and another from SLM-enriched `embed_text` per turn.
-- Retrieval can score against both and fuse (max-kind or blend) per
-- memory_search_scored_impl's vector_kind_strategy kwarg.
--
-- Default: 'default'. All existing rows migrate to this value so
-- pre-v022 callers continue to see a single embedding per memory_id
-- via (memory_id, embed_model, vector_kind='default').
--
-- Index: (memory_id, vector_kind) replaces idx_me_memory_id for the
-- new query shape. (memory_id, embed_model) stays in place for the
-- embed-model-filtered path. The v021 composite on
-- (content_hash, embed_model) is orthogonal and stays.

-- 1. Add the column with back-compat default for existing rows.
ALTER TABLE memory_embeddings ADD COLUMN vector_kind TEXT NOT NULL DEFAULT 'default';

-- 2. Composite index for the per-kind lookup pattern.
CREATE INDEX IF NOT EXISTS idx_me_memory_kind
    ON memory_embeddings(memory_id, vector_kind);

-- 3. Refresh stats after a schema change that affects a heavily-indexed table.
ANALYZE memory_embeddings;
