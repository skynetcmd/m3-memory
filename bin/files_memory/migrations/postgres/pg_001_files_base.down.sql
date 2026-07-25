-- pg_001_files_base.down.sql  (PostgreSQL)
-- Reverse of pg_001. Drop children before parents (FK order). No FTS on PG in
-- phase 1, so nothing FTS-related to drop. The `files` schema itself is left in
-- place (it may hold later-migration tables); drop it explicitly only if empty.

DROP TABLE IF EXISTS files.memory_links;
DROP TABLE IF EXISTS files.promotion_markers;
DROP TABLE IF EXISTS files.fact_entity_refs;
DROP TABLE IF EXISTS files.fact_embeddings;
DROP TABLE IF EXISTS files.facts;
DROP TABLE IF EXISTS files.file_embeddings;
DROP TABLE IF EXISTS files.leaf_embeddings;
DROP TABLE IF EXISTS files.leaves;
DROP TABLE IF EXISTS files.ingestion_runs;
DROP TABLE IF EXISTS files.file_nodes;
