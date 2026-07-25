-- 043_entity_coalesce.down.sql
-- Reverse of 043_entity_coalesce.up.sql.
--
-- NOTE: SQLite gained DROP COLUMN in 3.35 (2021). The runner targets a modern
-- SQLite, so the ADD COLUMNs are reversible here. If a very old SQLite is ever in
-- play, dropping the two coalesce tables + indexes is the material rollback; the
-- three nullable entities columns are harmless if they linger.
DROP TABLE IF EXISTS entity_coalesce_embeddings;
DROP INDEX IF EXISTS idx_ecc_reviewed;
DROP TABLE IF EXISTS entity_coalesce_candidates;
DROP INDEX IF EXISTS idx_entities_cluster;
DROP INDEX IF EXISTS idx_entities_coalesce_state;
ALTER TABLE entities DROP COLUMN resolution_run;
ALTER TABLE entities DROP COLUMN cluster_id;
ALTER TABLE entities DROP COLUMN coalesce_state;
