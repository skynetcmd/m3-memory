-- 043_entity_coalesce.up.sql
-- Provisional-entity coalescing pass: review queue + working-set columns +
-- name-hash embedding cache.
--
-- Follows 042.
--
-- BACKGROUND / bug this fixes. bin/files_memory/entity_coalesce.py created ALL of
-- its schema at RUNTIME via an inline ensure_schema() (CREATE TABLE IF NOT EXISTS
-- + guarded ALTER TABLE). That inline DDL was the ONLY source of these objects, so
-- (a) they never existed on PostgreSQL at all, and (b) SQLite carried a
-- runtime-CREATE pattern the rest of the store abandoned in favor of migrations —
-- schema drift waiting to happen. This migration makes the objects first-class on
-- BOTH backends; ensure_schema() degrades to a tolerant already-migrated fast-path.
--
-- NAME COLLISION this fixes. The coalescing cache was ALSO called
-- `entity_embeddings` — the SAME name as core migration 032's Tier-3 resolution
-- cache — but with an INCOMPATIBLE column set (name_hash + model vs 032's
-- embed_model). Both are keyed on entity_id, so on any store that ran 032 the two
-- subsystems would clobber each other's rows / raise `no such column`. It only ever
-- "worked" because the isolated coalesce test DB never applied 032. Following the
-- standard shared-DB remedy (a module-scoped table prefix — cf. Rails
-- table_name_prefix, Flask-AppBuilder's ab_* tables in Airflow/Superset), the
-- coalescing cache is renamed `entity_coalesce_embeddings`. Core 032's
-- `entity_embeddings` is left UNTOUCHED. The old rows (if any) were ephemeral,
-- rebuildable name-vectors — no data migration is needed.

-- Working-set columns on entities: an indexed provisional/quarantined/clustered
-- filter replaces the unindexable `attributes_json LIKE '%provisional%'` scan.
-- SQLite has no ADD COLUMN IF NOT EXISTS; the migrate runner applies each file
-- once (version-gated), so a bare ADD COLUMN is safe here.
ALTER TABLE entities ADD COLUMN coalesce_state TEXT;   -- 'provisional'|'quarantined'|'clustered'|NULL
ALTER TABLE entities ADD COLUMN cluster_id TEXT;
ALTER TABLE entities ADD COLUMN resolution_run TEXT;
CREATE INDEX IF NOT EXISTS idx_entities_coalesce_state ON entities(coalesce_state);
CREATE INDEX IF NOT EXISTS idx_entities_cluster ON entities(cluster_id);

-- Review queue: candidate coalescing pairs + their band/verdict/review action.
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

-- Name-hash-keyed embedding cache for the coalescing pass. Module-prefixed name
-- (entity_coalesce_embeddings) so it never collides with core 032's
-- entity_embeddings. embedding is a packed float32 BLOB (embedding_utils.pack);
-- name_hash lets a re-run skip re-embedding an unchanged name.
CREATE TABLE IF NOT EXISTS entity_coalesce_embeddings (
    entity_id   TEXT PRIMARY KEY,
    name_hash   TEXT NOT NULL,
    embedding   BLOB NOT NULL,
    dim         INTEGER NOT NULL,
    model       TEXT,
    created_at  TEXT
);
