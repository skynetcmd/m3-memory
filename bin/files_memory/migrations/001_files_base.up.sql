-- 001_files_base.up.sql  (SQLite)
--
-- files_memory phase-1 base schema, migrated from the inline schema.SCHEMA_V1
-- blob into a numbered file. Identical DDL — this is the SAME schema, just moved
-- from executescript(SCHEMA_V1) to a migration so SQLite and PostgreSQL evolve in
-- lock-step (see the PG mirror pg_001_files_base.up.sql, which omits the FTS5
-- section — FTS5 is SQLite-only and lands on PG as tsvector in Phase 2).
--
-- The runner wraps this file in one transaction; no BEGIN/COMMIT here. PRAGMAs
-- that must run outside a transaction (journal_mode) are applied by the
-- connection layer, NOT here.

-- ── file_nodes — the canonical "this file exists" record. One row per version.
CREATE TABLE IF NOT EXISTS file_nodes (
    uuid                TEXT PRIMARY KEY,
    identity_key        TEXT NOT NULL,
    filename            TEXT NOT NULL,
    filetype            TEXT NOT NULL,
    mime                TEXT,
    path_absolute       TEXT NOT NULL,
    path_repo_relative  TEXT,
    size_bytes          INTEGER NOT NULL,
    content_sha256      TEXT NOT NULL,
    date_created        TEXT,
    date_modified       TEXT NOT NULL,
    source_host         TEXT NOT NULL,
    version_label       TEXT NOT NULL,
    superseded_by       TEXT REFERENCES file_nodes(uuid),
    superseded_at       TEXT,
    supersession_reason TEXT,
    supersedes          TEXT REFERENCES file_nodes(uuid),
    paths_seen          TEXT,
    corpus_id           TEXT NOT NULL DEFAULT 'default',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    file_summary        TEXT,
    metadata            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_file_nodes_identity ON file_nodes(identity_key, superseded_by);
CREATE INDEX IF NOT EXISTS idx_file_nodes_corpus   ON file_nodes(corpus_id, superseded_by);
CREATE INDEX IF NOT EXISTS idx_file_nodes_sha      ON file_nodes(content_sha256);
CREATE INDEX IF NOT EXISTS idx_file_nodes_filetype ON file_nodes(filetype, superseded_by);
CREATE INDEX IF NOT EXISTS idx_file_nodes_path     ON file_nodes(path_absolute);

-- ── ingestion_runs — one per ingest invocation per file. Append-only.
CREATE TABLE IF NOT EXISTS ingestion_runs (
    uuid               TEXT PRIMARY KEY,
    file_node          TEXT NOT NULL REFERENCES file_nodes(uuid) ON DELETE CASCADE,
    run_id             TEXT NOT NULL,
    ingest_date        TEXT NOT NULL DEFAULT (datetime('now')),
    ingester_version   TEXT NOT NULL,
    chunker_version    TEXT NOT NULL,
    extractor_version  TEXT,
    extract_mode       TEXT NOT NULL,
    model_id           TEXT,
    chunk_count        INTEGER NOT NULL DEFAULT 0,
    leaf_count         INTEGER NOT NULL DEFAULT 0,
    fact_count         INTEGER NOT NULL DEFAULT 0,
    duration_ms        INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'ok',
    status_reason      TEXT,
    metadata           TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_runs_file_node ON ingestion_runs(file_node, ingest_date);
CREATE INDEX IF NOT EXISTS idx_runs_run_id    ON ingestion_runs(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_status    ON ingestion_runs(status);

-- ── leaves — the mined payload. Each leaf is a queryable chunk.
CREATE TABLE IF NOT EXISTS leaves (
    uuid                TEXT PRIMARY KEY,
    file_node           TEXT NOT NULL REFERENCES file_nodes(uuid) ON DELETE CASCADE,
    ingestion_run       TEXT NOT NULL REFERENCES ingestion_runs(uuid) ON DELETE CASCADE,
    division_type       TEXT NOT NULL,
    division_id         TEXT NOT NULL,
    division_label      TEXT,
    text                TEXT NOT NULL,
    text_sha256         TEXT NOT NULL,
    char_range_start    INTEGER NOT NULL,
    char_range_end      INTEGER NOT NULL,
    leaf_summary        TEXT,
    superseded_by       TEXT REFERENCES leaves(uuid),
    evolved_from        TEXT REFERENCES leaves(uuid),
    material_change     INTEGER,
    boundary_confidence REAL,
    truncated           INTEGER NOT NULL DEFAULT 0,
    extraction_status   TEXT NOT NULL DEFAULT 'pending',
    embedded            INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    metadata            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_leaves_file     ON leaves(file_node, superseded_by);
CREATE INDEX IF NOT EXISTS idx_leaves_sha      ON leaves(text_sha256);
CREATE INDEX IF NOT EXISTS idx_leaves_run      ON leaves(ingestion_run);
CREATE INDEX IF NOT EXISTS idx_leaves_division ON leaves(file_node, division_type, division_id);

-- ── FTS5 (SQLite-only). external-content, trigger-synced. The PG mirror omits
-- this entire section; Phase 2 replaces it with GIN-indexed tsvector columns.
CREATE VIRTUAL TABLE IF NOT EXISTS leaves_fts USING fts5(
    text,
    content='leaves',
    content_rowid='rowid',
    tokenize='porter unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS leaves_ai AFTER INSERT ON leaves BEGIN
    INSERT INTO leaves_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS leaves_ad AFTER DELETE ON leaves BEGIN
    INSERT INTO leaves_fts(leaves_fts, rowid, text) VALUES('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS leaves_au AFTER UPDATE ON leaves BEGIN
    INSERT INTO leaves_fts(leaves_fts, rowid, text) VALUES('delete', old.rowid, old.text);
    INSERT INTO leaves_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS file_summaries_fts USING fts5(
    filename, file_summary,
    content='file_nodes',
    content_rowid='rowid',
    tokenize='porter unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS file_summaries_ai AFTER INSERT ON file_nodes BEGIN
    INSERT INTO file_summaries_fts(rowid, filename, file_summary)
        VALUES (new.rowid, new.filename, COALESCE(new.file_summary, ''));
END;
CREATE TRIGGER IF NOT EXISTS file_summaries_ad AFTER DELETE ON file_nodes BEGIN
    INSERT INTO file_summaries_fts(file_summaries_fts, rowid, filename, file_summary)
        VALUES('delete', old.rowid, old.filename, COALESCE(old.file_summary, ''));
END;
CREATE TRIGGER IF NOT EXISTS file_summaries_au AFTER UPDATE ON file_nodes BEGIN
    INSERT INTO file_summaries_fts(file_summaries_fts, rowid, filename, file_summary)
        VALUES('delete', old.rowid, old.filename, COALESCE(old.file_summary, ''));
    INSERT INTO file_summaries_fts(rowid, filename, file_summary)
        VALUES (new.rowid, new.filename, COALESCE(new.file_summary, ''));
END;

-- ── Embeddings (BLOB storage).
CREATE TABLE IF NOT EXISTS leaf_embeddings (
    leaf_uuid    TEXT NOT NULL REFERENCES leaves(uuid) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    embedding    BLOB NOT NULL,
    embed_model  TEXT NOT NULL,
    dim          INTEGER NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (leaf_uuid, kind)
);
CREATE INDEX IF NOT EXISTS idx_leaf_embed_model ON leaf_embeddings(embed_model);

CREATE TABLE IF NOT EXISTS file_embeddings (
    file_node_uuid TEXT NOT NULL REFERENCES file_nodes(uuid) ON DELETE CASCADE,
    kind           TEXT NOT NULL DEFAULT 'summary',
    embedding      BLOB NOT NULL,
    embed_model    TEXT NOT NULL,
    dim            INTEGER NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (file_node_uuid, kind)
);
CREATE INDEX IF NOT EXISTS idx_file_embed_model ON file_embeddings(embed_model);

-- ── facts + fact_embeddings + fact_entity_refs (empty scaffolding in phase 1).
CREATE TABLE IF NOT EXISTS facts (
    uuid              TEXT PRIMARY KEY,
    leaf              TEXT NOT NULL REFERENCES leaves(uuid) ON DELETE CASCADE,
    file_node         TEXT NOT NULL REFERENCES file_nodes(uuid) ON DELETE CASCADE,
    statement         TEXT NOT NULL,
    source_span_start INTEGER NOT NULL,
    source_span_end   INTEGER NOT NULL,
    confidence        REAL NOT NULL DEFAULT 1.0,
    superseded_by     TEXT REFERENCES facts(uuid),
    extraction_run    TEXT NOT NULL REFERENCES ingestion_runs(uuid) ON DELETE CASCADE,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_facts_leaf ON facts(leaf);
CREATE INDEX IF NOT EXISTS idx_facts_file ON facts(file_node, superseded_by);

CREATE TABLE IF NOT EXISTS fact_embeddings (
    fact_uuid    TEXT PRIMARY KEY REFERENCES facts(uuid) ON DELETE CASCADE,
    embedding    BLOB NOT NULL,
    embed_model  TEXT NOT NULL,
    dim          INTEGER NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fact_entity_refs (
    fact         TEXT NOT NULL REFERENCES facts(uuid) ON DELETE CASCADE,
    entity_uuid  TEXT NOT NULL,
    confidence   REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (fact, entity_uuid)
);

-- ── promotion_markers (empty scaffolding in phase 1).
CREATE TABLE IF NOT EXISTS promotion_markers (
    uuid                TEXT PRIMARY KEY,
    source_memory       TEXT NOT NULL,
    source_memory_type  TEXT NOT NULL,
    promoted_to         TEXT NOT NULL,
    promoted_at         TEXT NOT NULL DEFAULT (datetime('now')),
    promoted_by         TEXT NOT NULL,
    reason              TEXT
);
CREATE INDEX IF NOT EXISTS idx_promotion_source ON promotion_markers(source_memory);

-- ── memory_links — generic edge table for the file_node tree graph.
CREATE TABLE IF NOT EXISTS memory_links (
    src_uuid    TEXT NOT NULL,
    dst_uuid    TEXT NOT NULL,
    edge_type   TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (src_uuid, dst_uuid, edge_type)
);
CREATE INDEX IF NOT EXISTS idx_memory_links_src ON memory_links(src_uuid, edge_type);
CREATE INDEX IF NOT EXISTS idx_memory_links_dst ON memory_links(dst_uuid, edge_type);
