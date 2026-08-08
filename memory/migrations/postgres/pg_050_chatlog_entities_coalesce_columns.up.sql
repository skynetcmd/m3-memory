-- pg_050_chatlog_entities_coalesce_columns.up.sql
--
-- Parity fix (continuation of pg_049): the CHATLOG container's `chat_log_entities`
-- clone was missing the entity-coalesce columns that pg_048 added to core
-- `entities` (coalesce_state, cluster_id, resolution_run).
--
-- Same root cause as pg_049: pg_043 cloned the chatlog tables from a snapshot of
-- core, and a column added to a core table AFTER that clone does not reach the
-- clone. On SQLite the chatlog is a separate DB FILE that runs the SAME migration
-- chain (043_entity_coalesce), so it gets these columns for free; on PG the clone
-- needs the explicit ALTER. Surfaced by test_chatlog_clones_mirror_core_columns.
--
-- ADD COLUMN IF NOT EXISTS is idempotent; migrate_pg wraps the file in one
-- implicit transaction.

ALTER TABLE chat_log_entities ADD COLUMN IF NOT EXISTS coalesce_state TEXT;
ALTER TABLE chat_log_entities ADD COLUMN IF NOT EXISTS cluster_id     TEXT;
ALTER TABLE chat_log_entities ADD COLUMN IF NOT EXISTS resolution_run TEXT;

CREATE INDEX IF NOT EXISTS idx_cl_entities_coalesce_state ON chat_log_entities(coalesce_state);
CREATE INDEX IF NOT EXISTS idx_cl_entities_cluster        ON chat_log_entities(cluster_id);
