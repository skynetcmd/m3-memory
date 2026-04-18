-- 001_bootstrap.up.sql
-- Bootstrap a separate chat-log DB with a full mirror of the main-DB schema.
-- Includes: memory_items (with all columns through v018), memory_embeddings,
-- memory_relationships, FTS5 virtual table with triggers, schema_versions tracker.

CREATE TABLE IF NOT EXISTS schema_versions (
    version INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    applied_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS memory_items (
    id                TEXT PRIMARY KEY,
    type              TEXT NOT NULL,
    title             TEXT,
    content           TEXT,
    metadata_json     TEXT,
    agent_id          TEXT,
    model_id          TEXT,
    change_agent      TEXT DEFAULT 'unknown',
    importance        REAL DEFAULT 0.5,
    source            TEXT DEFAULT 'agent',
    origin_device     TEXT DEFAULT 'macbook',
    is_deleted        INTEGER DEFAULT 0,
    expires_at        TEXT,
    decay_rate        REAL DEFAULT 0.0,
    created_at        TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at        TEXT,
    last_accessed_at  TEXT,
    access_count      INTEGER DEFAULT 0,
    user_id           TEXT,
    scope             TEXT,
    valid_from        TEXT,
    valid_to          TEXT,
    content_hash      TEXT,
    read_at           TEXT,
    conversation_id   TEXT,
    refresh_on        TEXT,
    refresh_reason    TEXT,
    variant           TEXT
);

CREATE TABLE IF NOT EXISTS memory_embeddings (
    id           TEXT PRIMARY KEY,
    memory_id    TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    embedding    BLOB NOT NULL,
    embed_model  TEXT DEFAULT 'jina-embeddings-v5',
    dim          INTEGER DEFAULT 1024,
    created_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    content_hash TEXT
);

CREATE TABLE IF NOT EXISTS memory_relationships (
    id                TEXT PRIMARY KEY,
    from_id           TEXT NOT NULL REFERENCES memory_items(id),
    to_id             TEXT NOT NULL REFERENCES memory_items(id),
    relationship_type TEXT NOT NULL,
    created_at        TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_items_fts USING fts5(
    title, content, content=memory_items, content_rowid=rowid
);

CREATE TRIGGER IF NOT EXISTS mi_fts_insert AFTER INSERT ON memory_items BEGIN
    INSERT INTO memory_items_fts(rowid, title, content)
    VALUES (new.rowid, new.title, new.content);
END;

CREATE TRIGGER IF NOT EXISTS mi_fts_delete AFTER DELETE ON memory_items BEGIN
    INSERT INTO memory_items_fts(memory_items_fts, rowid, title, content)
    VALUES ('delete', old.rowid, old.title, old.content);
END;

CREATE TRIGGER IF NOT EXISTS mi_fts_update AFTER UPDATE ON memory_items BEGIN
    INSERT INTO memory_items_fts(memory_items_fts, rowid, title, content)
    VALUES ('delete', old.rowid, old.title, old.content);
    INSERT INTO memory_items_fts(rowid, title, content)
    VALUES (new.rowid, new.title, new.content);
END;

CREATE INDEX IF NOT EXISTS idx_mi_type       ON memory_items(type);
CREATE INDEX IF NOT EXISTS idx_mi_agent      ON memory_items(agent_id);
CREATE INDEX IF NOT EXISTS idx_mi_model      ON memory_items(model_id);
CREATE INDEX IF NOT EXISTS idx_mi_created    ON memory_items(created_at);
CREATE INDEX IF NOT EXISTS idx_mi_deleted    ON memory_items(is_deleted);
CREATE INDEX IF NOT EXISTS idx_me_memory_id  ON memory_embeddings(memory_id);
CREATE INDEX IF NOT EXISTS idx_mr_from       ON memory_relationships(from_id);
CREATE INDEX IF NOT EXISTS idx_mr_to         ON memory_relationships(to_id);
CREATE INDEX IF NOT EXISTS idx_mi_conversation_composite
    ON memory_items(conversation_id, created_at)
    WHERE is_deleted = 0;
