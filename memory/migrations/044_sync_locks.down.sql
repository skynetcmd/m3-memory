-- 044_sync_locks.down.sql
-- Reverse of 044_sync_locks.up.sql: drop the sync lock table.
--
-- Safe to drop: it holds one transient row naming the host currently syncing,
-- not durable data. Worst case a sync running at the moment of the down-migration
-- loses its lock and a concurrent run is possible until the table returns.

DROP TABLE IF EXISTS sync_locks;
