-- 005_perf_and_wal.sql
-- Performance indexes, delta-sync watermarks, embedding content-hash cache.
-- All statements are additive (IF NOT EXISTS) — safe to re-run.

-- ── Missing indexes for common query patterns ────────────────────────────────

-- memory_search: most queries filter on (is_deleted, type) together
CREATE INDEX IF NOT EXISTS idx_mi_deleted_type ON memory_items(is_deleted, type);

-- memory_search / maintenance: importance-based pre-filter and decay
CREATE INDEX IF NOT EXISTS idx_mi_importance   ON memory_items(importance);

-- _push_to_chroma: queue drain ORDER BY queued_at ASC
CREATE INDEX IF NOT EXISTS idx_csq_queued_at   ON chroma_sync_queue(queued_at);

-- _pull_from_chroma: deletion marking WHERE is_deleted=0 AND is_local_origin=0
CREATE INDEX IF NOT EXISTS idx_cm_deleted_local ON chroma_mirror(is_deleted, is_local_origin);

-- sync_status: COUNT WHERE resolution = 'pending'
CREATE INDEX IF NOT EXISTS idx_sc_resolution   ON sync_conflicts(resolution);

-- memory_items updated_at for delta sync
CREATE INDEX IF NOT EXISTS idx_mi_updated      ON memory_items(updated_at);

-- ── Delta sync watermark table ───────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS sync_watermarks (
    direction   TEXT PRIMARY KEY,   -- 'pg_push', 'pg_pull'
    last_synced_at TEXT NOT NULL
);

-- ── Embedding content-hash cache column ──────────────────────────────────────
-- Allows skipping redundant embedding API calls for unchanged content.
-- ALTER TABLE ADD COLUMN is a no-op if the column already exists (SQLite ≥ 3.35).
-- Wrap in a safety check via the migration runner; if this fails the runner logs it.

ALTER TABLE memory_embeddings ADD COLUMN content_hash TEXT;
