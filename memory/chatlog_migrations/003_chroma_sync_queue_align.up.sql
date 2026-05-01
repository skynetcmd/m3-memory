-- 003_chroma_sync_queue_align.up.sql
--
-- Align chatlog DB's chroma_sync_queue with the canonical shape used by
-- the main DB (defined in memory/migrations/001_initial_schema.sql).
--
-- The chatlog DB historically created chroma_sync_queue lazily via
-- bin/m3_enrich.py with a minimal schema:
--   (id, memory_id, operation, enqueued_at)
--
-- The main DB has:
--   (id, memory_id, operation, attempts, stalled_since, queued_at)
--   plus indexes idx_csq_attempts and idx_csq_queued_at
--
-- Code in bin/memory_sync.py reads `attempts` (line 249) and treats it
-- as missing => "no such column: attempts" — non-fatal, but the queue
-- health check skips for the chatlog target. This migration brings
-- chatlog up to parity.
--
-- Strategy: SQLite cannot RENAME a column on every version we support,
-- but ADD COLUMN works on 3.16+. We:
--   1. Add `attempts` and `stalled_since` if missing.
--   2. Add `queued_at` (the canonical name) if missing — both columns
--      can coexist; existing rows keep `enqueued_at`, new rows pick up
--      `queued_at` via DEFAULT. Code reads `queued_at` going forward;
--      `enqueued_at` is left as a tombstone column.
--   3. Backfill `queued_at` from `enqueued_at` for existing rows so the
--      ORDER BY queued_at queries see the right timestamps.
--   4. Add the two missing indexes.
--
-- We deliberately do NOT drop `enqueued_at`. Hard-removing a column on
-- old SQLite means rebuilding the table; for a queue that drains
-- quickly, the dead column is cheaper than the rebuild risk.

-- SQLite ALTER TABLE ADD COLUMN requires a CONSTANT default. We can't
-- bind `strftime(...)` here, so the column is added nullable and we
-- backfill in two passes:
--   pass 1: copy `enqueued_at` into `queued_at` for existing rows
--   pass 2: stamp `now()` for any rows that had NULL enqueued_at
-- Application code (bin/memory_core.py INSERT INTO chroma_sync_queue ...)
-- supplies `queued_at` explicitly going forward, OR will after rebuilding
-- against this schema. New chatlog DBs ship the canonical shape via
-- bin/m3_enrich.py lazy-create which DOES use the strftime default.

-- On a fresh chatlog DB, chroma_sync_queue won't exist yet — the lazy
-- create fires only when memory_core writes through. Create it here in
-- the canonical shape so the ALTERs below have something to alter.
CREATE TABLE IF NOT EXISTS chroma_sync_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id     TEXT NOT NULL,
    operation     TEXT NOT NULL,
    enqueued_at   TEXT
);

ALTER TABLE chroma_sync_queue ADD COLUMN attempts INTEGER DEFAULT 0;
ALTER TABLE chroma_sync_queue ADD COLUMN stalled_since TEXT;
ALTER TABLE chroma_sync_queue ADD COLUMN queued_at TEXT;

UPDATE chroma_sync_queue
   SET queued_at = enqueued_at
 WHERE queued_at IS NULL AND enqueued_at IS NOT NULL;

UPDATE chroma_sync_queue
   SET queued_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
 WHERE queued_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_csq_attempts
    ON chroma_sync_queue(attempts);
CREATE INDEX IF NOT EXISTS idx_csq_queued_at
    ON chroma_sync_queue(queued_at);
