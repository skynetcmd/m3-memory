-- 016_task_tombstones.down.sql
-- Removes the tombstone column and its index.

DROP INDEX IF EXISTS idx_tasks_deleted_at;

ALTER TABLE tasks DROP COLUMN deleted_at;
