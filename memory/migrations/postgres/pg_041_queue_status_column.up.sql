-- pg_041_queue_status_column.up.sql
--
-- Add the `status` marker column ('done' | 'failed') to the extraction/enrichment
-- queues. On SQLite this column is added lazily at runtime
-- (m3_entities._ensure_extraction_status_column via PRAGMA table_info + ALTER);
-- on PG the schema is migration-managed, so the ported m3_entities / m3_enrich
-- can rely on it existing rather than doing SQLite-only runtime DDL.
--
-- ADD COLUMN IF NOT EXISTS is idempotent; one implicit transaction (migrate_pg
-- wraps the file), no explicit BEGIN/COMMIT.

ALTER TABLE entity_extraction_queue ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE fact_enrichment_queue   ADD COLUMN IF NOT EXISTS status TEXT;

CREATE INDEX IF NOT EXISTS idx_eeq_status ON entity_extraction_queue(status);
CREATE INDEX IF NOT EXISTS idx_feq_status ON fact_enrichment_queue(status);
