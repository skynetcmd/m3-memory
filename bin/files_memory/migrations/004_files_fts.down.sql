-- 004_files_fts.down.sql
-- Reverse of 004_files_fts.up.sql. That up-migration is a no-op on SQLite (the
-- FTS5 full-text tables come from 001_files_base), so its reversal is also a
-- no-op — dropping leaves_fts/file_summaries_fts here would wrongly tear down
-- schema owned by migration 001.
SELECT 1;
