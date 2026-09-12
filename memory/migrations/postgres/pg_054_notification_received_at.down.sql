-- pg_054_notification_received_at.down.sql
-- Reverses pg_054 by dropping received_at and its index.
--
-- Loses transport-receipt times but nothing about agent consumption: read_at is
-- a separate column and is untouched, so --unread_only behaves identically
-- before and after a rollback.

DROP INDEX IF EXISTS idx_notif_received_at;

ALTER TABLE notifications DROP COLUMN IF EXISTS received_at;
