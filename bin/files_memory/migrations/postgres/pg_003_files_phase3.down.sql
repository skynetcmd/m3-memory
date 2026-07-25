-- pg_003_files_phase3.down.sql  (PostgreSQL)
-- Reverse of pg_003. Dropping a table drops its indexes.

DROP TABLE IF EXISTS files.semantic_dedup_candidates;
DROP TABLE IF EXISTS files.fact_hit_stats;
