-- 005_drop_chroma_sync_queue.up.sql
-- Retires the ChromaDB sync feature on the chatlog DB. See the main-DB
-- counterpart memory/migrations/040_drop_chroma_sync_tables for the full
-- rationale. The chatlog DB only ever carried chroma_sync_queue (the mirror /
-- conflict / state tables were main-DB only), so this drop is the whole job.
--
-- Forward-only: chatlog migration 003 that ALTERs this table into canonical
-- shape is immutable applied history and is left untouched. Its indexes
-- (idx_csq_attempts, idx_csq_queued_at) drop with the table.

DROP TABLE IF EXISTS chroma_sync_queue;
