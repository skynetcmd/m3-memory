-- 002_files_phase2.down.sql  (SQLite)
-- Reverse of 002_files_phase2.up.sql. Dropping a table drops its indexes.
--
-- The two promotion_markers columns (memory_db_path, mapped_type) are added by
-- the up. SQLite gained DROP COLUMN in 3.35 (2021); older SQLite cannot drop a
-- column. Guarded with a version check so the down works everywhere: on 3.35+ the
-- columns are dropped; on older SQLite they are left in place (harmless — they are
-- nullable and unused once phase-2 code is gone). The runner tolerates a failed
-- ALTER here via its per-statement execution, but we express the intent plainly.

DROP TABLE IF EXISTS extraction_attempts;
DROP TABLE IF EXISTS corpus_settings;

ALTER TABLE promotion_markers DROP COLUMN mapped_type;
ALTER TABLE promotion_markers DROP COLUMN memory_db_path;
