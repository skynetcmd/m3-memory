-- 032_entity_embeddings — store-once entity-name vectors for resolution.
--
-- Tier-3 entity resolution (embedding cosine) previously re-embedded up to 100
-- candidate canonical_names on every cold resolve. At scale the in-process
-- name-embed cache thrashes past its MAX and clears, so each new entity re-embeds
-- ~100 candidates — O(candidates) embed calls dominate wall time. Persisting each
-- entity's name vector ONCE lets resolution load candidate vectors from this
-- table instead of re-embedding them — a cold resolve drops from ~101 embed
-- calls to 1 (just the new query name).
--
-- One row per entity. embedding is a packed float32 blob (embedding_utils.pack),
-- embed_model + dim guard against mixing vectors from different embedders.
CREATE TABLE IF NOT EXISTS entity_embeddings (
    entity_id   TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    embedding   BLOB NOT NULL,
    embed_model TEXT,
    dim         INTEGER,
    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
