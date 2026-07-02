-- 038_entity_extraction_status.down.sql
-- Reverts 038_entity_extraction_status.up.sql.

DROP INDEX IF EXISTS idx_eeq_status_done;

ALTER TABLE entity_extraction_queue DROP COLUMN status;
