-- 023_fact_enrichment_queue.up.sql
--
-- Create the fact_enrichment_queue table to hold memory items pending
-- SLM-driven fact extraction. Used by the write path to enqueue work
-- when the semaphore is saturated, and by the nightly drain to retry
-- failed extractions with backoff.
--
-- UNIQUE on memory_id prevents duplicate queue entries.
-- ON DELETE CASCADE removes queue rows when source items are deleted.
-- Indexes: (memory_id) for dedup checks, (attempts, enqueued_at) for
-- query ordering (queue first, then retry candidates, then eligible items).
--
-- Hardening: all operations use IF NOT EXISTS to ensure idempotence
-- on already-migrated DBs.

CREATE TABLE IF NOT EXISTS fact_enrichment_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id     TEXT NOT NULL,
    enqueued_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    attempts      INTEGER DEFAULT 0,
    last_error    TEXT,
    last_attempt_at TEXT,
    FOREIGN KEY(memory_id) REFERENCES memory_items(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_feq_memory_id ON fact_enrichment_queue(memory_id);
CREATE INDEX IF NOT EXISTS idx_feq_attempts ON fact_enrichment_queue(attempts, enqueued_at);
