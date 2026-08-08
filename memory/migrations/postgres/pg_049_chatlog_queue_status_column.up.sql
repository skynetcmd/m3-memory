-- pg_049_chatlog_queue_status_column.up.sql
--
-- Parity fix: the CHATLOG container's extraction queue was missing the `status`
-- marker column that its CORE counterpart has.
--
-- pg_041 added `status` to entity_extraction_queue (core). pg_043 then CLONED the
-- chatlog tables (chat_log_*) from a definition that predated `status`, so
-- chat_log_extraction_queue shipped WITHOUT it. On SQLite the column is added
-- lazily at runtime (m3_entities._ensure_extraction_status_column) for BOTH the
-- core file and the separate chatlog file; on PG the schema is migration-managed,
-- so the chatlog clone needs this explicit ALTER.
--
-- Consequence of the gap: chatlog entity extraction on PG could not record a turn
-- as terminal ('done'/'ctx_error'/'failed'), so has_entity_work never went False
-- for the chatlog store and the cognitive loop could not idle on PG. (The gate
-- itself degrades safely via the dialect's column_exists primitive; this restores
-- the convergence the marker enables.)
--
-- ADD COLUMN IF NOT EXISTS is idempotent; migrate_pg wraps the file in one
-- implicit transaction (no explicit BEGIN/COMMIT).

ALTER TABLE chat_log_extraction_queue ADD COLUMN IF NOT EXISTS status TEXT;

CREATE INDEX IF NOT EXISTS idx_cleq_status ON chat_log_extraction_queue(status);
