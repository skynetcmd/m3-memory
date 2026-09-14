-- pg_056_notification_lease.down.sql
-- Reverses pg_056. Delivery and consumption are untouched: received_at and
-- read_at are separate columns, so --unread_only behaves identically after.

DROP INDEX IF EXISTS idx_notif_conversation;
DROP INDEX IF EXISTS idx_notif_expired_lease;

ALTER TABLE notifications DROP COLUMN IF EXISTS reply_to_id;
ALTER TABLE notifications DROP COLUMN IF EXISTS conversation_id;
ALTER TABLE notifications DROP COLUMN IF EXISTS failed_at;
ALTER TABLE notifications DROP COLUMN IF EXISTS attempt_count;
ALTER TABLE notifications DROP COLUMN IF EXISTS lease_token;
ALTER TABLE notifications DROP COLUMN IF EXISTS claim_expires_at;
