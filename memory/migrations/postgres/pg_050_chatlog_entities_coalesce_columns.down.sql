-- pg_050_chatlog_entities_coalesce_columns.down.sql — revert pg_050.
DROP INDEX IF EXISTS idx_cl_entities_cluster;
DROP INDEX IF EXISTS idx_cl_entities_coalesce_state;
ALTER TABLE chat_log_entities DROP COLUMN IF EXISTS resolution_run;
ALTER TABLE chat_log_entities DROP COLUMN IF EXISTS cluster_id;
ALTER TABLE chat_log_entities DROP COLUMN IF EXISTS coalesce_state;
