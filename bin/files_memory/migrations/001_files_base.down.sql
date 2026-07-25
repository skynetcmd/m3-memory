-- 001_files_base.down.sql  (SQLite)
-- Reverse of 001_files_base.up.sql. Drop in FK-safe order (children before
-- parents). Dropping a table drops its indexes; dropping the base table drops
-- its FTS5 external-content shadow is NOT automatic, so drop the FTS + triggers
-- explicitly first. IF EXISTS makes this safe on a partial schema.

-- FTS5 virtual tables + their sync triggers (SQLite-only).
DROP TRIGGER IF EXISTS file_summaries_au;
DROP TRIGGER IF EXISTS file_summaries_ad;
DROP TRIGGER IF EXISTS file_summaries_ai;
DROP TABLE   IF EXISTS file_summaries_fts;
DROP TRIGGER IF EXISTS leaves_au;
DROP TRIGGER IF EXISTS leaves_ad;
DROP TRIGGER IF EXISTS leaves_ai;
DROP TABLE   IF EXISTS leaves_fts;

-- Edge + scaffolding tables.
DROP TABLE IF EXISTS memory_links;
DROP TABLE IF EXISTS promotion_markers;
DROP TABLE IF EXISTS fact_entity_refs;
DROP TABLE IF EXISTS fact_embeddings;
DROP TABLE IF EXISTS facts;

-- Embeddings.
DROP TABLE IF EXISTS file_embeddings;
DROP TABLE IF EXISTS leaf_embeddings;

-- Core chain: leaves -> ingestion_runs -> file_nodes (children first).
DROP TABLE IF EXISTS leaves;
DROP TABLE IF EXISTS ingestion_runs;
DROP TABLE IF EXISTS file_nodes;
