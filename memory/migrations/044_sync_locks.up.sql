-- 044_sync_locks.up.sql
--
-- Declares the sync lock as a table with a name that describes what it holds,
-- and retires pg_sync's runtime CREATE TABLE for it.
--
-- HISTORY (why this is not just a rename):
-- pg_sync's single-instance lock had no table of its own. It borrowed
-- `sync_state` — a ChromaDB federation table (per-collection pull/push cursors)
-- created by 001 — and stored the lock in it under the sentinel key
-- 'pg_sync_lock', with the lock value in a column called `last_pull_at`.
--
-- Migration 040 then retired the Chroma feature and DROPped `sync_state`, which
-- was correct for Chroma and unknowingly removed the lock's home. The lock
-- SELECT began raising "no such table", a bare-except read that as "another sync
-- is in progress", and EVERY sync silently skipped — for good. That is the
-- 2026-07-19 stale-warehouse outage. The fix at the time was an idempotent
-- CREATE TABLE inside pg_sync, which worked but left the lock living in a table
-- 040 had deliberately removed, under column names that describe Chroma.
--
-- So: a purpose-named table instead of resurrecting `sync_state`. 040's drop
-- stays meaningful, and the columns say what they contain.
--
-- ⚠ NOT REPLICATED. This is per-MACHINE state: it records that THIS host is
-- mid-sync. Copying it to the warehouse would let one machine's lock block or
-- be stolen by another. `pg_sync`'s table list must never include it.
--
-- `holder` is `host|pid`, not a bare pid. A pid is only meaningful on the host
-- that wrote it, so on a shared store (a PostgreSQL primary two machines both
-- sync from) a bare pid lets machine A read machine B's live pid, find no such
-- local process, judge the lock stale and steal it -> two concurrent syncs.
-- Recording the host lets a reader tell "not my host, cannot evaluate" from
-- "my host, process is gone", and fall back to the staleness ceiling for the
-- former.

CREATE TABLE IF NOT EXISTS sync_locks (
    lock_name   TEXT PRIMARY KEY,
    holder      TEXT,
    acquired_at TEXT
);
