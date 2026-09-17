-- 001_bootstrap.down.sql
--
-- Reverses the dispatch bootstrap. Because the dispatch store is a SEPARATE
-- database, rolling back here touches nothing in agent_memory.db or
-- agent_chatlog.db -- that isolation is one of the reasons for the split.
--
-- ⚠ WHAT THIS DISCARDS. Undelivered agent mail. The PAYLOAD survives: it lives
-- in `memory_items` in the main store and this store only ever carried a
-- `memory_id` pointing at it. What is lost is the DELIVERY RECORD, so a
-- recipient who had not yet acted is never told the message existed.
--
-- On a store that is still empty (schema created, nothing dispatched yet) this
-- is a no-op beyond dropping the tables. Once the handoff notify leg dispatches
-- here, treat it as data loss and drain the queue first.
--
-- Dropping the whole database file is the other way to reverse this, and for a
-- store whose entire contents are ephemeral it is often the right one. This
-- file exists so the migration runner has a symmetric down step.

DROP INDEX IF EXISTS idx_deliberation_created;
DROP INDEX IF EXISTS idx_deliberation_dispatch;
DROP TABLE IF EXISTS deliberation_log;

DROP INDEX IF EXISTS idx_dispatch_conversation;
DROP INDEX IF EXISTS idx_dispatch_expired_lease;
DROP INDEX IF EXISTS idx_dispatch_agent_unread;
DROP TABLE IF EXISTS notification_dispatch;

-- Dropped LAST, matching memory/chatlog_migrations/001_bootstrap.down.sql:10.
-- A bootstrap's down step returns the file to empty, so the next `up` runs from
-- a clean slate rather than replaying against a version tracker that claims the
-- schema is already present.
DROP TABLE IF EXISTS schema_versions;
