-- pg_049_chatlog_queue_status_column.down.sql — revert pg_049.
DROP INDEX IF EXISTS idx_cleq_status;
ALTER TABLE chat_log_extraction_queue DROP COLUMN IF EXISTS status;
