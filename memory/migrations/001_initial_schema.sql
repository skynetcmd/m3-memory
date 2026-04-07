-- 001_initial_schema.sql
-- Baseline schema to initialize the database or catch up an existing one.
-- Uses IF NOT EXISTS to be safe to run on existing DBs.

CREATE TABLE IF NOT EXISTS activity_logs (
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    query TEXT,
    response TEXT,
    model_used TEXT DEFAULT 'DeepSeek-R1-70B'
);

CREATE VIEW IF NOT EXISTS thinking_stream AS
SELECT 
    strftime('%H:%M:%S', timestamp) AS time,
    query AS "Project",
    CASE 
        WHEN response LIKE '%<think>%' THEN 
            substr(substr(response, instr(response, '<think>') + 7), 1, 500) || '...'
        ELSE substr(response, 1, 500) || '...'
    END AS "Reasoning Preview",
    length(response) AS "Full Size"
FROM activity_logs;

CREATE TABLE IF NOT EXISTS project_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT,
    decision TEXT,
    rationale TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS hardware_specs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component TEXT,
    spec TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS system_focus (
    id INTEGER PRIMARY KEY,
    summary TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS memory_items (
    id            TEXT PRIMARY KEY,
    type          TEXT NOT NULL,
    title         TEXT,
    content       TEXT,
    metadata_json TEXT,
    agent_id      TEXT,
    model_id      TEXT,
    change_agent  TEXT DEFAULT 'unknown',
    importance    REAL DEFAULT 0.5,
    source        TEXT DEFAULT 'agent',
    origin_device TEXT DEFAULT 'macbook',
    is_deleted    INTEGER DEFAULT 0,
    expires_at    TEXT,
    decay_rate    REAL DEFAULT 0.0,
    created_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS memory_embeddings (
    id          TEXT PRIMARY KEY,
    memory_id   TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    embedding   BLOB NOT NULL,
    embed_model TEXT DEFAULT 'jina-embeddings-v5',
    dim         INTEGER DEFAULT 1024,
    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS memory_relationships (
    id                TEXT PRIMARY KEY,
    from_id           TEXT NOT NULL REFERENCES memory_items(id),
    to_id             TEXT NOT NULL REFERENCES memory_items(id),
    relationship_type TEXT NOT NULL,
    created_at        TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

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

-- Note: We do NOT use CREATE INDEX IF NOT EXISTS here natively because older SQLite versions might lack IF NOT EXISTS for indexes.
-- For safety we will let the python script handle index creation idempotency or assume they exist.
-- Actually SQLite >= 3.3.0 supports IF NOT EXISTS for indexes.
CREATE INDEX IF NOT EXISTS idx_mi_type       ON memory_items(type);
CREATE INDEX IF NOT EXISTS idx_mi_agent      ON memory_items(agent_id);
CREATE INDEX IF NOT EXISTS idx_mi_model      ON memory_items(model_id);
CREATE INDEX IF NOT EXISTS idx_mi_created    ON memory_items(created_at);
CREATE INDEX IF NOT EXISTS idx_mi_deleted    ON memory_items(is_deleted);
CREATE INDEX IF NOT EXISTS idx_me_memory_id  ON memory_embeddings(memory_id);
CREATE INDEX IF NOT EXISTS idx_mr_from       ON memory_relationships(from_id);
CREATE INDEX IF NOT EXISTS idx_mr_to         ON memory_relationships(to_id);
CREATE INDEX IF NOT EXISTS idx_csq_attempts  ON chroma_sync_queue(attempts);
