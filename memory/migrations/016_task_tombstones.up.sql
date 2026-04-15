-- 016_task_tombstones.up.sql
-- Adds a tombstone column to `tasks` so deletes can propagate through pg_sync
-- as ordinary UPSERTs keyed on updated_at. Soft-delete sets deleted_at; reads
-- filter it out. Hard-delete still removes the row locally, but cross-peer
-- hard-delete is not a sync concept (sync is UPSERT-only) — peers converge via
-- the soft-delete tombstone.

ALTER TABLE tasks ADD COLUMN deleted_at TEXT DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_tasks_deleted_at ON tasks(deleted_at);
