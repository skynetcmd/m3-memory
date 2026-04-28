-- 003_chroma_sync_queue_align.down.sql
--
-- Reverse migration 003. Drops the indexes and columns added when
-- aligning chatlog's chroma_sync_queue with the main shape.
--
-- SQLite >= 3.35 supports ALTER TABLE DROP COLUMN. The queue is drain-
-- and-delete so dropping columns is safe even mid-flight: at most we
-- lose attempts/stalled_since metadata for in-flight rows.

DROP INDEX IF EXISTS idx_csq_attempts;
DROP INDEX IF EXISTS idx_csq_queued_at;

ALTER TABLE chroma_sync_queue DROP COLUMN queued_at;
ALTER TABLE chroma_sync_queue DROP COLUMN stalled_since;
ALTER TABLE chroma_sync_queue DROP COLUMN attempts;
