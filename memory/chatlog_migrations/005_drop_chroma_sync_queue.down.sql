-- 005_drop_chroma_sync_queue.down.sql
-- Recreates chroma_sync_queue on the chatlog DB in the canonical shape (matching
-- the post-003 schema) so the migration is reversible. Data is NOT restored (an
-- irreversible DROP); only the empty structure and its indexes return.

CREATE TABLE IF NOT EXISTS chroma_sync_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id     TEXT NOT NULL,
    operation     TEXT NOT NULL,
    enqueued_at   TEXT,
    attempts      INTEGER DEFAULT 0,
    stalled_since TEXT,
    queued_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_csq_attempts   ON chroma_sync_queue(attempts);
CREATE INDEX IF NOT EXISTS idx_csq_queued_at  ON chroma_sync_queue(queued_at);
