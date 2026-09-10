-- pg_053_sync_state_tables.down.sql — revert pg_053.
--
-- ⚠ Dropping sync_watermarks discards every delta cursor on this machine. The
-- next sync has no watermark and therefore does a FULL reconcile of every table
-- against the warehouse — correct, but potentially slow and heavy on a large
-- store. No data is lost: watermarks are derived state, rebuilt by syncing.
--
-- sync_locks holds one transient row and is safe to drop; a sync in flight at
-- that moment loses its lock, so a concurrent run is possible until it returns.

DROP TABLE IF EXISTS sync_locks;
DROP TABLE IF EXISTS sync_watermarks;
