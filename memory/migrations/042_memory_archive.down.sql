-- 042_memory_archive.down.sql
-- Reverse of 042_memory_archive.up.sql: drop the memory_archive table + indexes.

DROP INDEX IF EXISTS idx_memory_archive_at;
DROP INDEX IF EXISTS idx_memory_archive_reason;
DROP INDEX IF EXISTS idx_memory_archive_user;
DROP TABLE IF EXISTS memory_archive;
