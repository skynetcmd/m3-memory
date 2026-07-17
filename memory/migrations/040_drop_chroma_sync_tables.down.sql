-- 040_drop_chroma_sync_tables.down.sql
-- Recreates the ChromaDB federation/sync tables dropped by 040.up so the
-- migration is reversible. Schemas are copied verbatim from 001_initial_schema
-- (tables) and 005_perf_and_wal (indexes) — the historical source of truth. Data
-- is NOT restored (an irreversible DROP); only the empty structures return, which
-- is the correct semantics for a down-migration of a feature removal.

CREATE TABLE IF NOT EXISTS chroma_sync_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id     TEXT NOT NULL,
    operation     TEXT NOT NULL,
    attempts      INTEGER DEFAULT 0,
    stalled_since TEXT,
    queued_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS chroma_mirror (
    id                TEXT PRIMARY KEY,
    type              TEXT,
    title             TEXT,
    content           TEXT,
    metadata_json     TEXT,
    agent_id          TEXT,
    model_id          TEXT,
    change_agent      TEXT DEFAULT 'unknown',
    origin_device     TEXT,
    importance        REAL DEFAULT 0.5,
    is_deleted        INTEGER DEFAULT 0,
    remote_created_at TEXT,
    remote_updated_at TEXT,
    pulled_at         TEXT NOT NULL,
    is_local_origin   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chroma_mirror_embeddings (
    id        TEXT PRIMARY KEY,
    mirror_id TEXT NOT NULL REFERENCES chroma_mirror(id) ON DELETE CASCADE,
    embedding BLOB NOT NULL,
    dim       INTEGER DEFAULT 768,
    pulled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_conflicts (
    id              TEXT PRIMARY KEY,
    memory_id       TEXT NOT NULL,
    local_content   TEXT,
    remote_content  TEXT,
    local_updated   TEXT,
    remote_updated  TEXT,
    local_device    TEXT,
    remote_device   TEXT,
    local_agent     TEXT DEFAULT 'unknown',
    remote_agent    TEXT DEFAULT 'unknown',
    local_model_id  TEXT,
    remote_model_id TEXT,
    resolution      TEXT DEFAULT 'pending',
    resolved_at     TEXT,
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS sync_state (
    collection_name TEXT PRIMARY KEY,
    last_pull_at    TEXT,
    last_push_at    TEXT,
    items_pulled    INTEGER DEFAULT 0,
    items_pushed    INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_csq_attempts      ON chroma_sync_queue(attempts);
CREATE INDEX IF NOT EXISTS idx_csq_queued_at     ON chroma_sync_queue(queued_at);
CREATE INDEX IF NOT EXISTS idx_cm_deleted_local  ON chroma_mirror(is_deleted, is_local_origin);
CREATE INDEX IF NOT EXISTS idx_sc_resolution     ON sync_conflicts(resolution);
