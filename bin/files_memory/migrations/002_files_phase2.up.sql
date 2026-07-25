-- 002_files_phase2.up.sql  (SQLite)
--
-- files_memory phase-2 additions (corpus_settings, extraction_attempts,
-- promotion_markers.memory_db_path + mapped_type).
-- Generated from schema.SCHEMA_V2 + db._apply_v2's ALTERs. Follows 001.
--
-- The runner wraps this file in one transaction; no BEGIN/COMMIT here.
-- Connection-level PRAGMAs (journal_mode etc.) are applied by the
-- connection layer, not here. Version tracking is the runner's job, so
-- the schema_migrations bookkeeping from the blob is intentionally omitted.

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
-- promotion_markers gains memory_db_path + mapped_type (from db._apply_v2). In the
-- live code these are PRAGMA-guarded because SQLite lacks ADD COLUMN IF NOT EXISTS;
-- under the migration runner's version gate the columns are guaranteed absent on
-- first application, so a plain ADD COLUMN is correct.
ALTER TABLE promotion_markers ADD COLUMN memory_db_path TEXT;
ALTER TABLE promotion_markers ADD COLUMN mapped_type TEXT;
