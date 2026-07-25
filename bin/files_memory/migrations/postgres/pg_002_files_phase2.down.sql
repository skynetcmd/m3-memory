-- pg_002_files_phase2.down.sql  (PostgreSQL)
-- Reverse of pg_002. PG supports DROP COLUMN cleanly (unlike old SQLite).

DROP TABLE IF EXISTS files.extraction_attempts;
DROP TABLE IF EXISTS files.corpus_settings;

ALTER TABLE files.promotion_markers DROP COLUMN IF EXISTS mapped_type;
ALTER TABLE files.promotion_markers DROP COLUMN IF EXISTS memory_db_path;
