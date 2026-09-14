-- 047_notification_lease.down.sql
-- Reverses 047. Requires SQLite >= 3.35 for DROP COLUMN (same floor as 014/045/046).
--
-- Rolling back loses leases, fencing tokens, attempt counts and threading, but
-- nothing about delivery or consumption: received_at and read_at are untouched,
-- so --unread_only behaves identically before and after. Rows claimed under 046
-- keep their claimed_by; they simply revert to having no expiry.

DROP INDEX IF EXISTS idx_notif_conversation;
DROP INDEX IF EXISTS idx_notif_expired_lease;

ALTER TABLE notifications DROP COLUMN reply_to_id;
ALTER TABLE notifications DROP COLUMN conversation_id;
ALTER TABLE notifications DROP COLUMN failed_at;
ALTER TABLE notifications DROP COLUMN attempt_count;
ALTER TABLE notifications DROP COLUMN lease_token;
ALTER TABLE notifications DROP COLUMN claim_expires_at;
