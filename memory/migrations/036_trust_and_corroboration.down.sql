-- 036_trust_and_corroboration.down.sql
-- Reverts 036_trust_and_corroboration.up.sql.

DROP INDEX IF EXISTS idx_corrob_memory_source;
DROP INDEX IF EXISTS idx_corrob_memory;
DROP TABLE IF EXISTS memory_corroborations;

ALTER TABLE agents DROP COLUMN trust_score;
