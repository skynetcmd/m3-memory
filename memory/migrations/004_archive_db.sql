-- 003_archive_db.sql
-- Records that the archive database (agent_memory_archive.db) is now in use.
-- The archive DB is initialized separately by memory_bridge.py at startup.
-- This migration is a no-op marker so the version is tracked in schema_versions.

SELECT 1; -- intentional no-op
