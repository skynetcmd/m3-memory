-- pg_053_sync_state_tables.up.sql
--
-- Declares the two tables sync creates AT RUNTIME, so a PostgreSQL primary has
-- them by migration like every other table rather than by whichever code path
-- happened to run first.
--
-- WHY THEY WERE UNDECLARED (both are real incidents, not hypotheticals):
--
--   sync_watermarks — created ad-hoc in TWO places with DIFFERENT SQL:
--     bin/pg_sync.py's _ensure_watermark_table (qmark placeholders, SQLite side)
--     and bin/sync_all.py's FDW fast-path (%s, PG side). Two writers, one shape,
--     no owner. It exists in the SQLite schema via 005_perf_and_wal.sql but was
--     never mirrored here, so on PG it only ever existed if the right code ran.
--
--   the sync LOCK — had no table of its own at all. It borrowed `sync_state`, a
--     ChromaDB federation table, under the key 'pg_sync_lock'. Migration 040
--     retired Chroma and dropped `sync_state`; the lock SELECT started raising
--     "no such table", a bare-except read that as "another sync in progress",
--     and EVERY sync silently skipped thereafter (the 2026-07-19 stale-warehouse
--     outage). SQLite migration 044 gives it a purpose-named home, `sync_locks`;
--     this is its PostgreSQL half.
--
-- ⚠⚠ NEITHER TABLE IS EVER REPLICATED TO THE WAREHOUSE.
-- Both are PER-MACHINE, PER-PEER state: a watermark records how far THIS host
-- has synced against THAT remote, and a lock records that THIS host is mid-sync.
-- Replicating them would make two machines share one cursor — each resuming from
-- the other's position, re-syncing or skipping rows — and share one lock, so one
-- machine blocks or steals the other's. pg_sync's table list must continue to
-- exclude both. This is the one place where replication is the bug, which is why
-- it is stated here rather than left to a reader to infer from an absence.
--
-- Types follow the schema's conventions (TEXT timestamps as written by the
-- callers, which pass ISO-8601 strings). CREATE TABLE IF NOT EXISTS is
-- idempotent and safe on a store where the ad-hoc DDL already ran; migrate_pg
-- wraps the file in one implicit transaction.

CREATE TABLE IF NOT EXISTS sync_watermarks (
    direction      TEXT PRIMARY KEY,
    last_synced_at TEXT
);

-- `holder` is `host|pid`, not a bare pid: a pid is only meaningful on the host
-- that wrote it. On a PostgreSQL primary that two machines both sync from, they
-- share this row — and a bare pid lets machine A read machine B's live pid, find
-- no such local process, judge the lock stale and steal it, giving two concurrent
-- syncs. Recording the host lets a reader distinguish "not my host, cannot
-- evaluate" from "my host, the process is gone".
CREATE TABLE IF NOT EXISTS sync_locks (
    lock_name   TEXT PRIMARY KEY,
    holder      TEXT,
    acquired_at TEXT
);
