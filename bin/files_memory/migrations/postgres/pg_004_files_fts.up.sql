-- pg_004_files_fts.up.sql
--
-- Full-text search parity for the files store on PostgreSQL — the tsvector/GIN
-- analogue of SQLite's FTS5 leaves_fts / file_summaries_fts (migration 001).
--
-- BACKGROUND. pg_001_files_base OMITTED full-text: FTS5 virtual tables + their
-- six sync triggers are SQLite-only, and the PG equivalent was deferred to
-- "Phase 2". Until now, files_search's keyword channel had NO PG implementation
-- (files_index likewise), so hybrid search on a PG files store ran vector-only.
-- This adds the native PG full-text surface so the keyword channel works on both
-- backends.
--
-- DESIGN (decision 2026-07-25): native full-text only — NO ParadeDB/pg_search
-- extension dependency (that needs superuser + shared_preload_libraries + a
-- non-core Rust extension, which breaks the vanilla/air-gapped/federal-PG floor
-- and m3 cannot bundle a server-side extension the way it bundles SQLite's FTS5).
-- So: a GENERATED tsvector column + GIN index, exactly like core memory_items'
-- search_vector. A GENERATED ALWAYS ... STORED column self-maintains on every
-- write — NO triggers (the FTS5 side needs 6; PG does it declaratively). Queried
-- with `search_vector @@ to_tsquery('english', ...)`, ranked with ts_rank (the
-- files search path negates it to the bm25 lower-is-better convention). If a
-- deployment ever installs pg_search, a BM25 arm can be added later behind the
-- seam's keyword_accelerator probe — never required.
--
-- English config mirrors the SQLite FTS5 'porter' stemmer intent (Snowball
-- English == Porter). coalesce() guards NULL text/summary (to_tsvector(NULL) is
-- NULL, which a GENERATED expression rejects). migrate runs this in one txn.

-- leaves: full-text over the mined chunk text (the files_search keyword channel).
ALTER TABLE files.leaves
    ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(text, ''))
    ) STORED;
CREATE INDEX IF NOT EXISTS idx_leaves_search_vector
    ON files.leaves USING GIN (search_vector);

-- file_nodes: full-text over filename + file_summary (powers files_index keyword
-- filtering). filename weighted 'A' over the summary 'B' so a filename hit
-- outranks a summary hit — mirroring the file_summaries_fts intent.
ALTER TABLE files.file_nodes
    ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(filename, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(file_summary, '')), 'B')
    ) STORED;
CREATE INDEX IF NOT EXISTS idx_file_nodes_search_vector
    ON files.file_nodes USING GIN (search_vector);
