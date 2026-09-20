-- 050: index memory_embeddings(content_hash, embed_model).
--
-- The embed cache looks up by exactly this pair -- see
-- `SELECT embedding, embed_model FROM memory_embeddings WHERE content_hash = ?
--  AND embed_model = ?` in bin/memory/embed.py -- and there was no index for
-- it. memory_items.content_hash has been indexed since 016; the embeddings
-- table was missed.
--
-- Measured cost of the gap (2026-09-20, 136k-row chatlog store): a dedup query
-- joining this table to itself on content_hash ran past a 10-minute timeout
-- twice. Creating this index took 0.6s and the same work then completed in 3s.
-- Every cache lookup was paying a scan of the same shape, just smaller.
--
-- Composite rather than content_hash alone: the pair is what the cache probe
-- filters on, so this serves it as a covering index for the lookup, and it
-- still serves a content_hash-only predicate as a leftmost-prefix match.
CREATE INDEX IF NOT EXISTS idx_me_content_hash_model
    ON memory_embeddings(content_hash, embed_model);
