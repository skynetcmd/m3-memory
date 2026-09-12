-- 045_notification_received_at.down.sql
-- Reverses 045 by dropping received_at and its index.
-- Requires SQLite >= 3.35 for ALTER TABLE DROP COLUMN (same floor as 014).
--
-- Dropping this loses transport-receipt times but nothing about agent
-- consumption: read_at is a separate column and is untouched, so --unread_only
-- behaves identically before and after a rollback.

DROP INDEX IF EXISTS idx_notif_received_at;

ALTER TABLE notifications DROP COLUMN received_at;
