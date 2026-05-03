-- 031_enrichment_send_to.down.sql
--
-- Reverses 031_enrichment_send_to.up.sql.

DROP INDEX IF EXISTS idx_eg_send_to;
ALTER TABLE enrichment_groups DROP COLUMN send_to;
