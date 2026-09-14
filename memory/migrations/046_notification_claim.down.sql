-- 046_notification_claim.down.sql
-- Reverses 046 by dropping the claim columns and their index.
-- Requires SQLite >= 3.35 for ALTER TABLE DROP COLUMN (same floor as 014/045).
--
-- Rolling back loses WHICH instance held a row, but nothing about delivery or
-- consumption: received_at and read_at are separate columns and are untouched,
-- so --unread_only behaves identically before and after.

DROP INDEX IF EXISTS idx_notif_claimable;

ALTER TABLE notifications DROP COLUMN claimed_at;
ALTER TABLE notifications DROP COLUMN claimed_by;
