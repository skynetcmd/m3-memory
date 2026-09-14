-- pg_055_notification_claim.down.sql
-- Reverses pg_055 by dropping the claim columns and their index.
--
-- Rolling back loses WHICH instance held a row, but nothing about delivery or
-- consumption: received_at and read_at are separate columns and are untouched.

DROP INDEX IF EXISTS idx_notif_claimable;

ALTER TABLE notifications DROP COLUMN IF EXISTS claimed_at;
ALTER TABLE notifications DROP COLUMN IF EXISTS claimed_by;
