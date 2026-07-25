-- 003_files_phase3.down.sql  (SQLite)
-- Reverse of 003_files_phase3.up.sql. Dropping a table drops its indexes.

DROP TABLE IF EXISTS semantic_dedup_candidates;
DROP TABLE IF EXISTS fact_hit_stats;
