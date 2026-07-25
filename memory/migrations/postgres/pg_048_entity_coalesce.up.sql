-- pg_048_entity_coalesce.up.sql
--
-- PostgreSQL mirror of SQLite migration 043_entity_coalesce.up.sql — the
-- provisional-entity coalescing schema (review queue + entities working-set
-- columns + name-hash embedding cache). See the SQLite migration header for the
-- full background: these objects were previously created ONLY by a runtime inline
-- ensure_schema(), so they never existed on PG, and the coalescing cache collided
-- by name with core 032's entity_embeddings. The cache is renamed
-- entity_coalesce_embeddings (module prefix) to remove the collision; core
-- entity_embeddings is left untouched.
--
-- Type mapping mirrors the SQLite intent: TEXT->TEXT, REAL->REAL,
-- INTEGER->INTEGER, BLOB->BYTEA (packed float32, DB-blind Rust cosine, same as
-- entity_embeddings/memory_embeddings). PG has ADD COLUMN IF NOT EXISTS so the
-- entities columns are idempotent; migrate_pg wraps the file in one transaction.

-- Working-set columns on entities (indexed provisional/quarantined/clustered
-- filter, replacing the unindexable attributes_json LIKE scan).
ALTER TABLE entities ADD COLUMN IF NOT EXISTS coalesce_state TEXT;
ALTER TABLE entities ADD COLUMN IF NOT EXISTS cluster_id TEXT;
ALTER TABLE entities ADD COLUMN IF NOT EXISTS resolution_run TEXT;
CREATE INDEX IF NOT EXISTS idx_entities_coalesce_state ON entities(coalesce_state);
CREATE INDEX IF NOT EXISTS idx_entities_cluster ON entities(cluster_id);

-- Review queue.
CREATE TABLE IF NOT EXISTS entity_coalesce_candidates (
    uuid            TEXT PRIMARY KEY,
    entity_a        TEXT NOT NULL,
    entity_b        TEXT NOT NULL,
    name_a          TEXT,
    name_b          TEXT,
    cosine          REAL,
    fuzzy           INTEGER,
    band            TEXT,               -- 'merge' | 'needs_llm'
    verdict         TEXT,               -- 'merge' | 'related_not_same' | 'uncertain' | NULL
    resolution_run  TEXT,
    detected_at     TEXT,
    reviewed_at     TEXT,
    review_action   TEXT,               -- 'merge' | 'related' | 'reject' | 'defer' | 'unapplied'
    metadata        TEXT
);
CREATE INDEX IF NOT EXISTS idx_ecc_reviewed ON entity_coalesce_candidates(reviewed_at);

-- Name-hash-keyed coalescing embedding cache (module-prefixed; distinct from core
-- entity_embeddings). BLOB -> BYTEA.
CREATE TABLE IF NOT EXISTS entity_coalesce_embeddings (
    entity_id   TEXT PRIMARY KEY,
    name_hash   TEXT NOT NULL,
    embedding   BYTEA NOT NULL,
    dim         INTEGER NOT NULL,
    model       TEXT,
    created_at  TEXT
);
