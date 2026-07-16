-- pg_041_queue_status_column.down.sql — revert pg_041.
DROP INDEX IF EXISTS idx_feq_status;
DROP INDEX IF EXISTS idx_eeq_status;
ALTER TABLE fact_enrichment_queue   DROP COLUMN IF EXISTS status;
ALTER TABLE entity_extraction_queue DROP COLUMN IF EXISTS status;
