"""SQL DDL for files.db (schema version 1).

Inline rather than file-based migrations because phase 1 is a clean slate
— no legacy DB to migrate from. When schema evolves, add migrations as
`files_memory/migrations/NNN_*.up.sql` and switch db._lazy_init to that
pattern (mirroring `memory/migrations/`).

Tables:
  file_nodes          one row per file version (supersession chain)
  ingestion_runs      one row per ingest invocation per file
  leaves              one row per leaf (page/slide/heading/chunk), embedded
  facts               EMPTY in phase 1 (created for schema stability)
  fact_entity_refs    EMPTY in phase 1
  promotion_markers   EMPTY in phase 1
  memory_links        generic edge table (parent/child/supersedes/evolved)
  leaf_embeddings     vector storage (BLOB) keyed by (leaf_uuid, kind)
  file_embeddings     vector storage for file_summary
  schema_migrations   schema version tracking

Plus FTS5 virtual tables:
  leaves_fts          full-text index over leaves.text
  file_summaries_fts  full-text index over file_nodes.file_summary

Conventions matched from bin/memory/:
  - UUID primary keys as TEXT
  - JSON columns stored as TEXT (json1 extension enabled)
  - ISO 8601 strings for timestamps (TEXT)
  - BLOB for embeddings (per memory_embeddings precedent)
"""
from __future__ import annotations

SCHEMA_V1 = r"""
-- ─────────────────────────────────────────────────────────────────────────────
-- Settings (run by db._lazy_init on a fresh DB)
-- ─────────────────────────────────────────────────────────────────────────────
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA temp_store = MEMORY;
PRAGMA mmap_size = 268435456;  -- 256 MiB; matches memory.db defaults

-- ─────────────────────────────────────────────────────────────────────────────
-- Schema version tracking
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now')),
    description TEXT
);

-- ─────────────────────────────────────────────────────────────────────────────
-- file_nodes — the canonical "this file exists" record. One row per version.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS file_nodes (
    uuid                TEXT PRIMARY KEY,
    identity_key        TEXT NOT NULL,           -- usually path; m3_doc_id if declared
    filename            TEXT NOT NULL,
    filetype            TEXT NOT NULL,           -- normalized: 'markdown', 'pdf', 'text', ...
    mime                TEXT,
    path_absolute       TEXT NOT NULL,
    path_repo_relative  TEXT,
    size_bytes          INTEGER NOT NULL,
    content_sha256      TEXT NOT NULL,
    date_created        TEXT,                    -- fs ctime / birthtime, ISO 8601
    date_modified       TEXT NOT NULL,           -- fs mtime, ISO 8601
    source_host         TEXT NOT NULL,
    version_label       TEXT NOT NULL,           -- 'ingest-1', user override, or frontmatter
    superseded_by       TEXT REFERENCES file_nodes(uuid),
    superseded_at       TEXT,
    supersession_reason TEXT,
    supersedes          TEXT REFERENCES file_nodes(uuid),
    paths_seen          TEXT,                    -- JSON array of paths the same content lived at
    corpus_id           TEXT NOT NULL DEFAULT 'default',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    file_summary        TEXT,                    -- nullable until summarizer runs
    metadata            TEXT NOT NULL DEFAULT '{}'  -- JSON extensible blob
);

CREATE INDEX IF NOT EXISTS idx_file_nodes_identity
    ON file_nodes(identity_key, superseded_by);
CREATE INDEX IF NOT EXISTS idx_file_nodes_corpus
    ON file_nodes(corpus_id, superseded_by);
CREATE INDEX IF NOT EXISTS idx_file_nodes_sha
    ON file_nodes(content_sha256);
CREATE INDEX IF NOT EXISTS idx_file_nodes_filetype
    ON file_nodes(filetype, superseded_by);
CREATE INDEX IF NOT EXISTS idx_file_nodes_path
    ON file_nodes(path_absolute);

-- ─────────────────────────────────────────────────────────────────────────────
-- ingestion_runs — one per ingest invocation per file. Append-only.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ingestion_runs (
    uuid               TEXT PRIMARY KEY,
    file_node          TEXT NOT NULL REFERENCES file_nodes(uuid) ON DELETE CASCADE,
    run_id             TEXT NOT NULL,           -- shared across all files in one walk
    ingest_date        TEXT NOT NULL DEFAULT (datetime('now')),
    ingester_version   TEXT NOT NULL,
    chunker_version    TEXT NOT NULL,
    extractor_version  TEXT,
    extract_mode       TEXT NOT NULL,           -- 'none' | 'inline' | 'queue'
    model_id           TEXT,
    chunk_count        INTEGER NOT NULL DEFAULT 0,
    leaf_count         INTEGER NOT NULL DEFAULT 0,
    fact_count         INTEGER NOT NULL DEFAULT 0,
    duration_ms        INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'ok',    -- 'ok'|'partial'|'failed'|'unchanged_skipped'
    status_reason      TEXT,
    metadata           TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_runs_file_node
    ON ingestion_runs(file_node, ingest_date);
CREATE INDEX IF NOT EXISTS idx_runs_run_id
    ON ingestion_runs(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_status
    ON ingestion_runs(status);

-- ─────────────────────────────────────────────────────────────────────────────
-- leaves — the mined payload. Each leaf is a queryable chunk.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS leaves (
    uuid                TEXT PRIMARY KEY,
    file_node           TEXT NOT NULL REFERENCES file_nodes(uuid) ON DELETE CASCADE,
    ingestion_run       TEXT NOT NULL REFERENCES ingestion_runs(uuid) ON DELETE CASCADE,
    division_type       TEXT NOT NULL,           -- 'page'|'slide'|'heading'|'function'|'row_range'|'window'|'cell'
    division_id         TEXT NOT NULL,           -- '4', 'slide-12', 'intro/methods', ...
    division_label      TEXT,                    -- human-readable
    text                TEXT NOT NULL,
    text_sha256         TEXT NOT NULL,
    char_range_start    INTEGER NOT NULL,
    char_range_end      INTEGER NOT NULL,
    leaf_summary        TEXT,                    -- nullable; set for coarse leaves only
    superseded_by       TEXT REFERENCES leaves(uuid),
    evolved_from        TEXT REFERENCES leaves(uuid),
    material_change     INTEGER,                 -- 0/1; meaningful only if evolved_from set
    boundary_confidence REAL,                    -- 0.0 - 1.0
    truncated           INTEGER NOT NULL DEFAULT 0,
    extraction_status   TEXT NOT NULL DEFAULT 'pending',  -- 'pending'|'ok'|'failed'|'skipped'
    embedded            INTEGER NOT NULL DEFAULT 0,       -- 0/1; set when leaf_embeddings row exists
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    metadata            TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_leaves_file
    ON leaves(file_node, superseded_by);
CREATE INDEX IF NOT EXISTS idx_leaves_sha
    ON leaves(text_sha256);
CREATE INDEX IF NOT EXISTS idx_leaves_run
    ON leaves(ingestion_run);
CREATE INDEX IF NOT EXISTS idx_leaves_division
    ON leaves(file_node, division_type, division_id);

-- FTS5 over leaves.text. external-content so we don't double-store; sync
-- via triggers.
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

-- FTS5 over file_nodes.file_summary (powers files_index keyword filtering).
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

-- ─────────────────────────────────────────────────────────────────────────────
-- Embeddings (BLOB storage; same pattern as memory_embeddings).
-- One row per (leaf_uuid, kind) where kind ∈ {'text','summary'}.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS leaf_embeddings (
    leaf_uuid    TEXT NOT NULL REFERENCES leaves(uuid) ON DELETE CASCADE,
    kind         TEXT NOT NULL,                -- 'text' | 'summary'
    embedding    BLOB NOT NULL,                -- packed float32 vector
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

-- ─────────────────────────────────────────────────────────────────────────────
-- facts — created empty in phase 1 (extraction lands in phase 2)
-- ─────────────────────────────────────────────────────────────────────────────
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
    entity_uuid  TEXT NOT NULL,                 -- lives in memory.db; not a FK
    confidence   REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (fact, entity_uuid)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- promotion_markers — created empty in phase 1 (ascension lands in phase 2)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS promotion_markers (
    uuid                TEXT PRIMARY KEY,
    source_memory       TEXT NOT NULL,         -- UUID in files.db
    source_memory_type  TEXT NOT NULL,         -- 'fact'|'leaf'|'file_summary'
    promoted_to         TEXT NOT NULL,         -- UUID in memory.db (cross-DB; not a FK)
    promoted_at         TEXT NOT NULL DEFAULT (datetime('now')),
    promoted_by         TEXT NOT NULL,
    reason              TEXT
);
CREATE INDEX IF NOT EXISTS idx_promotion_source ON promotion_markers(source_memory);

-- ─────────────────────────────────────────────────────────────────────────────
-- memory_links — generic edge table for the file_node tree graph
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS memory_links (
    src_uuid    TEXT NOT NULL,
    dst_uuid    TEXT NOT NULL,
    edge_type   TEXT NOT NULL,                 -- 'parent'|'evolved_from'|'supersedes'|'belongs_to_ingestion'|...
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (src_uuid, dst_uuid, edge_type)
);
CREATE INDEX IF NOT EXISTS idx_memory_links_src ON memory_links(src_uuid, edge_type);
CREATE INDEX IF NOT EXISTS idx_memory_links_dst ON memory_links(dst_uuid, edge_type);

-- ─────────────────────────────────────────────────────────────────────────────
-- Initial migration record
-- ─────────────────────────────────────────────────────────────────────────────
INSERT OR IGNORE INTO schema_migrations(version, description)
    VALUES (1, 'phase 1: file_nodes, ingestion_runs, leaves, embeddings, FTS5, empty fact/promotion scaffolding');
"""

# ─────────────────────────────────────────────────────────────────────────────
# v2 (phase 2): activate extraction, ascension, staleness paths.
# All ADDITIVE. No table renames or column drops.
# ─────────────────────────────────────────────────────────────────────────────
SCHEMA_V2 = r"""
-- corpus_settings: per-corpus defaults (extract mode, scope, etc).
-- Free-form JSON; readers fall back to global defaults when unset.
CREATE TABLE IF NOT EXISTS corpus_settings (
    corpus_id    TEXT PRIMARY KEY,
    settings     TEXT NOT NULL DEFAULT '{}',     -- JSON
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- extraction_attempts: per-leaf attempt log. Lets staleness review
-- surface leaves that failed extraction with their reasons. Many-to-one
-- with leaves so retries don't overwrite prior attempt history.
CREATE TABLE IF NOT EXISTS extraction_attempts (
    uuid               TEXT PRIMARY KEY,
    leaf_uuid          TEXT NOT NULL REFERENCES leaves(uuid) ON DELETE CASCADE,
    ingestion_run      TEXT NOT NULL REFERENCES ingestion_runs(uuid) ON DELETE CASCADE,
    extractor_version  TEXT NOT NULL,
    model_id           TEXT,
    attempted_at       TEXT NOT NULL DEFAULT (datetime('now')),
    status             TEXT NOT NULL,            -- 'ok'|'failed'|'skipped_size'|'skipped_type'
    fact_count         INTEGER NOT NULL DEFAULT 0,
    duration_ms        INTEGER NOT NULL DEFAULT 0,
    error              TEXT
);
CREATE INDEX IF NOT EXISTS idx_extraction_leaf ON extraction_attempts(leaf_uuid, attempted_at);
CREATE INDEX IF NOT EXISTS idx_extraction_status ON extraction_attempts(status);

-- promotion_markers gets a 'memory_db_path' column so we can find the
-- target across multi-DB setups. Falls back to NULL = the active M3Context's
-- default DB. ALTER TABLE ADD COLUMN works in SQLite without trickery.
-- The column might already exist if v2 ran before; guard via PRAGMA check
-- in db._apply_v2.

INSERT OR IGNORE INTO schema_migrations(version, description)
    VALUES (2, 'phase 2: corpus_settings, extraction_attempts, promotion_markers.memory_db_path');
"""
