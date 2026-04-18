-- 001_bootstrap.down.sql
-- Nukes the bootstrap schema. Only used when abandoning the separate chat log DB entirely.
DROP TRIGGER IF EXISTS mi_fts_update;
DROP TRIGGER IF EXISTS mi_fts_delete;
DROP TRIGGER IF EXISTS mi_fts_insert;
DROP TABLE IF EXISTS memory_items_fts;
DROP TABLE IF EXISTS memory_relationships;
DROP TABLE IF EXISTS memory_embeddings;
DROP TABLE IF EXISTS memory_items;
DROP TABLE IF EXISTS schema_versions;
