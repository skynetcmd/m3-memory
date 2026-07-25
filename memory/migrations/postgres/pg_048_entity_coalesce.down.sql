-- pg_048_entity_coalesce.down.sql
-- Reverse of pg_048_entity_coalesce.up.sql: drop the coalescing tables/indexes
-- and the entities working-set columns.
DROP TABLE IF EXISTS entity_coalesce_embeddings;
DROP INDEX IF EXISTS idx_ecc_reviewed;
DROP TABLE IF EXISTS entity_coalesce_candidates;
DROP INDEX IF EXISTS idx_entities_cluster;
DROP INDEX IF EXISTS idx_entities_coalesce_state;
ALTER TABLE entities DROP COLUMN IF EXISTS resolution_run;
ALTER TABLE entities DROP COLUMN IF EXISTS cluster_id;
ALTER TABLE entities DROP COLUMN IF EXISTS coalesce_state;
