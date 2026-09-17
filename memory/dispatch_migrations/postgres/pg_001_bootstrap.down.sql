-- pg_001_bootstrap.down.sql
--
-- Reverses the PostgreSQL dispatch bootstrap. Runs against whatever schema the
-- caller has set on `search_path`, exactly as the up step does -- the DDL is
-- deliberately unqualified so one script serves any schema name.
--
-- ⚠ WHAT THIS DISCARDS. Undelivered agent mail. The PAYLOAD survives: it lives
-- in `memory_items` in the main schema and this store only ever carried a
-- `memory_id` pointing at it. What is lost is the DELIVERY RECORD, so a
-- recipient who had not yet acted is never told the message existed.
--
-- On a store that is still empty this is a no-op beyond dropping the tables.
-- Once the handoff notify leg dispatches here, treat it as data loss and drain
-- the queue first.
--
-- ⚠ DOES NOT DROP THE SCHEMA ITSELF. The schema is created by the caller, not
-- by the up step, so it is not this file's to remove -- and a DROP SCHEMA
-- CASCADE here would take anything else the operator had put there with it.
-- Reversing the schema creation is the caller's job.
--
-- Indexes are dropped explicitly before their tables even though DROP TABLE
-- would remove them, so this file reads line for line against the SQLite
-- counterpart: a reviewer comparing the two sees intent rather than dialect.

DROP INDEX IF EXISTS idx_deliberation_created;
DROP INDEX IF EXISTS idx_deliberation_dispatch;
DROP TABLE IF EXISTS deliberation_log;

DROP INDEX IF EXISTS idx_dispatch_conversation;
DROP INDEX IF EXISTS idx_dispatch_expired_lease;
DROP INDEX IF EXISTS idx_dispatch_agent_unread;
DROP TABLE IF EXISTS notification_dispatch;

-- Dropped last, matching the SQLite counterpart: a bootstrap's down step
-- returns the store to empty so the next `up` runs from a clean slate.
DROP TABLE IF EXISTS schema_versions;
