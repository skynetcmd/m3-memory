-- pg_001_files_base.up.sql  (PostgreSQL)
--
-- PostgreSQL mirror of SQLite 001_files_base.up.sql. The files store is a SCHEMA
-- NAMESPACE (`files`) inside the ONE primary database (single DSN) — NOT a separate
-- database. Every table is created in that schema; application code addresses them
-- via the qualified_table()/files_table() seam primitive (never search_path — see
-- ~/.m3-private/plans/FILES_DB_TO_PG_PLAN.md).
--
-- Type mapping (SQLite -> PG): TEXT->TEXT, INTEGER->BIGINT, REAL->DOUBLE PRECISION,
-- BLOB->BYTEA, datetime('now') default -> NOW()/TIMESTAMPTZ.
--
-- FTS5 IS OMITTED. leaves_fts / file_summaries_fts + their six triggers are
-- SQLite-only; PG full-text search (GIN-indexed tsvector) lands in Phase 2. Until
-- then files_search on PG runs the vector channel only.
--
-- CREATE SCHEMA/TABLE/INDEX IF NOT EXISTS keep this idempotent; migrate_pg wraps
-- the file in one transaction.

CREATE SCHEMA IF NOT EXISTS files;

-- ── file_nodes
CREATE TABLE IF NOT EXISTS files.file_nodes (
    uuid                TEXT PRIMARY KEY,
    identity_key        TEXT NOT NULL,
    filename            TEXT NOT NULL,
    filetype            TEXT NOT NULL,
    mime                TEXT,
    path_absolute       TEXT NOT NULL,
    path_repo_relative  TEXT,
    size_bytes          BIGINT NOT NULL,
    content_sha256      TEXT NOT NULL,
    date_created        TEXT,
    date_modified       TEXT NOT NULL,
    source_host         TEXT NOT NULL,
    version_label       TEXT NOT NULL,
    superseded_by       TEXT REFERENCES files.file_nodes(uuid),
    superseded_at       TEXT,
    supersession_reason TEXT,
    supersedes          TEXT REFERENCES files.file_nodes(uuid),
    paths_seen          TEXT,
    corpus_id           TEXT NOT NULL DEFAULT 'default',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    file_summary        TEXT,
    metadata            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_file_nodes_identity ON files.file_nodes(identity_key, superseded_by);
CREATE INDEX IF NOT EXISTS idx_file_nodes_corpus   ON files.file_nodes(corpus_id, superseded_by);
CREATE INDEX IF NOT EXISTS idx_file_nodes_sha      ON files.file_nodes(content_sha256);
CREATE INDEX IF NOT EXISTS idx_file_nodes_filetype ON files.file_nodes(filetype, superseded_by);
CREATE INDEX IF NOT EXISTS idx_file_nodes_path     ON files.file_nodes(path_absolute);

-- ── ingestion_runs
CREATE TABLE IF NOT EXISTS files.ingestion_runs (
    uuid               TEXT PRIMARY KEY,
    file_node          TEXT NOT NULL REFERENCES files.file_nodes(uuid) ON DELETE CASCADE,
    run_id             TEXT NOT NULL,
    ingest_date        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ingester_version   TEXT NOT NULL,
    chunker_version    TEXT NOT NULL,
    extractor_version  TEXT,
    extract_mode       TEXT NOT NULL,
    model_id           TEXT,
    chunk_count        BIGINT NOT NULL DEFAULT 0,
    leaf_count         BIGINT NOT NULL DEFAULT 0,
    fact_count         BIGINT NOT NULL DEFAULT 0,
    duration_ms        BIGINT NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'ok',
    status_reason      TEXT,
    metadata           TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_runs_file_node ON files.ingestion_runs(file_node, ingest_date);
CREATE INDEX IF NOT EXISTS idx_runs_run_id    ON files.ingestion_runs(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_status    ON files.ingestion_runs(status);

-- ── leaves
CREATE TABLE IF NOT EXISTS files.leaves (
    uuid                TEXT PRIMARY KEY,
    file_node           TEXT NOT NULL REFERENCES files.file_nodes(uuid) ON DELETE CASCADE,
    ingestion_run       TEXT NOT NULL REFERENCES files.ingestion_runs(uuid) ON DELETE CASCADE,
    division_type       TEXT NOT NULL,
    division_id         TEXT NOT NULL,
    division_label      TEXT,
    text                TEXT NOT NULL,
    text_sha256         TEXT NOT NULL,
    char_range_start    BIGINT NOT NULL,
    char_range_end      BIGINT NOT NULL,
    leaf_summary        TEXT,
    superseded_by       TEXT REFERENCES files.leaves(uuid),
    evolved_from        TEXT REFERENCES files.leaves(uuid),
    material_change     BIGINT,
    boundary_confidence DOUBLE PRECISION,
    truncated           BIGINT NOT NULL DEFAULT 0,
    extraction_status   TEXT NOT NULL DEFAULT 'pending',
    embedded            BIGINT NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata            TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_leaves_file     ON files.leaves(file_node, superseded_by);
CREATE INDEX IF NOT EXISTS idx_leaves_sha      ON files.leaves(text_sha256);
CREATE INDEX IF NOT EXISTS idx_leaves_run      ON files.leaves(ingestion_run);
CREATE INDEX IF NOT EXISTS idx_leaves_division ON files.leaves(file_node, division_type, division_id);

-- ── embeddings (BLOB -> BYTEA)
CREATE TABLE IF NOT EXISTS files.leaf_embeddings (
    leaf_uuid    TEXT NOT NULL REFERENCES files.leaves(uuid) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    embedding    BYTEA NOT NULL,
    embed_model  TEXT NOT NULL,
    dim          BIGINT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (leaf_uuid, kind)
);
CREATE INDEX IF NOT EXISTS idx_leaf_embed_model ON files.leaf_embeddings(embed_model);

CREATE TABLE IF NOT EXISTS files.file_embeddings (
    file_node_uuid TEXT NOT NULL REFERENCES files.file_nodes(uuid) ON DELETE CASCADE,
    kind           TEXT NOT NULL DEFAULT 'summary',
    embedding      BYTEA NOT NULL,
    embed_model    TEXT NOT NULL,
    dim            BIGINT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (file_node_uuid, kind)
);
CREATE INDEX IF NOT EXISTS idx_file_embed_model ON files.file_embeddings(embed_model);

-- ── facts + fact_embeddings + fact_entity_refs (empty scaffolding in phase 1)
CREATE TABLE IF NOT EXISTS files.facts (
    uuid              TEXT PRIMARY KEY,
    leaf              TEXT NOT NULL REFERENCES files.leaves(uuid) ON DELETE CASCADE,
    file_node         TEXT NOT NULL REFERENCES files.file_nodes(uuid) ON DELETE CASCADE,
    statement         TEXT NOT NULL,
    source_span_start BIGINT NOT NULL,
    source_span_end   BIGINT NOT NULL,
    confidence        DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    superseded_by     TEXT REFERENCES files.facts(uuid),
    extraction_run    TEXT NOT NULL REFERENCES files.ingestion_runs(uuid) ON DELETE CASCADE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_facts_leaf ON files.facts(leaf);
CREATE INDEX IF NOT EXISTS idx_facts_file ON files.facts(file_node, superseded_by);

CREATE TABLE IF NOT EXISTS files.fact_embeddings (
    fact_uuid    TEXT PRIMARY KEY REFERENCES files.facts(uuid) ON DELETE CASCADE,
    embedding    BYTEA NOT NULL,
    embed_model  TEXT NOT NULL,
    dim          BIGINT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS files.fact_entity_refs (
    fact         TEXT NOT NULL REFERENCES files.facts(uuid) ON DELETE CASCADE,
    entity_uuid  TEXT NOT NULL,
    confidence   DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    PRIMARY KEY (fact, entity_uuid)
);

-- ── promotion_markers (empty scaffolding in phase 1)
CREATE TABLE IF NOT EXISTS files.promotion_markers (
    uuid                TEXT PRIMARY KEY,
    source_memory       TEXT NOT NULL,
    source_memory_type  TEXT NOT NULL,
    promoted_to         TEXT NOT NULL,
    promoted_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    promoted_by         TEXT NOT NULL,
    reason              TEXT
);
CREATE INDEX IF NOT EXISTS idx_promotion_source ON files.promotion_markers(source_memory);

-- ── memory_links — generic edge table for the file_node tree graph
CREATE TABLE IF NOT EXISTS files.memory_links (
    src_uuid    TEXT NOT NULL,
    dst_uuid    TEXT NOT NULL,
    edge_type   TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (src_uuid, dst_uuid, edge_type)
);
CREATE INDEX IF NOT EXISTS idx_memory_links_src ON files.memory_links(src_uuid, edge_type);
CREATE INDEX IF NOT EXISTS idx_memory_links_dst ON files.memory_links(dst_uuid, edge_type);
