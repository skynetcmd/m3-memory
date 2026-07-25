-- pg_002_files_phase2.up.sql  (PostgreSQL)
-- Mirror of SQLite 002_files_phase2.up.sql. Follows pg_001.
-- corpus_settings + extraction_attempts + promotion_markers.{memory_db_path,
-- mapped_type}. Type mapping per pg_001's header.

CREATE TABLE IF NOT EXISTS files.corpus_settings (
    corpus_id    TEXT PRIMARY KEY,
    settings     TEXT NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS files.extraction_attempts (
    uuid               TEXT PRIMARY KEY,
    leaf_uuid          TEXT NOT NULL REFERENCES files.leaves(uuid) ON DELETE CASCADE,
    ingestion_run      TEXT NOT NULL REFERENCES files.ingestion_runs(uuid) ON DELETE CASCADE,
    extractor_version  TEXT NOT NULL,
    model_id           TEXT,
    attempted_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status             TEXT NOT NULL,
    fact_count         BIGINT NOT NULL DEFAULT 0,
    duration_ms        BIGINT NOT NULL DEFAULT 0,
    error              TEXT
);
CREATE INDEX IF NOT EXISTS idx_extraction_leaf   ON files.extraction_attempts(leaf_uuid, attempted_at);
CREATE INDEX IF NOT EXISTS idx_extraction_status ON files.extraction_attempts(status);

ALTER TABLE files.promotion_markers ADD COLUMN IF NOT EXISTS memory_db_path TEXT;
ALTER TABLE files.promotion_markers ADD COLUMN IF NOT EXISTS mapped_type TEXT;
